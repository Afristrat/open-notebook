"""Ingestion d'une veille Saqr: classement des sources, extraits d'articles, contenu du podcast.

Fonctions pures, sans reseau ni base: l'extraction elle-meme (content_process) vit dans
la commande qui orchestre. Les regles viennent de la recette du 30/09 au 02/10/2026:

- Diwan lit lui-meme chaque source et ne fait pas confiance a ce que Saqr annonce;
- une source inexploitable (echec, paywall, page qui ne correspond pas au titre) est
  une ALTERATION; au-dela de 10 %, pas de podcast (decision d'Amine du 01/10);
- les chiffres que le texte de Saqr tire du CORPS d'un article (et non de son resume)
  doivent exister dans le corpus, sinon le controle de contenu refuse: on ajoute donc
  au contenu les phrases de l'article complet qui portent les nombres de la veille.
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree

from open_notebook.podcasts.content_guard import SOURCES_DELIMITER

MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
    "septembre", "octobre", "novembre", "décembre",
]
PAYWALL_MARKERS = (
    "abonnez-vous", "réservé aux abonnés", "article réservé", "subscribe to continue",
    "subscribe to read", "sign in to continue", "log in to continue", "access denied",
    "403 forbidden", "enable javascript", "verify you are human",
)
BOILERPLATE = (
    "cookie", "privacy policy", "consent", "storage preferences", "subscribe",
    "abonnez", "newsletter", "opens an external", "all rights reserved",
)
MIN_TEXT_CHARS = 300
SHORT_FORM_MIN_CHARS = 80

INGEREE = "ingeree"
REPRISE = "reprise"
ECHEC = "echec"
PAYWALL = "paywall"
DIVERGENT = "divergent"
ALTERED = (ECHEC, PAYWALL, DIVERGENT)

_REF_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")
_ARXIV_ABS = re.compile(r"arxiv\.org/abs/([\w.\-/]+)", re.I)


@dataclass
class SourceResult:
    """Une source de la veille apres lecture par Diwan."""

    n: int
    statut: str
    detail: str
    titre: str
    editeur: str
    meta: str
    url: str
    texte: str = ""

    def report(self) -> Dict[str, Any]:
        """Version sans texte, destinee au rapport conserve en base."""
        return {
            "n": self.n, "statut": self.statut, "detail": self.detail, "titre": self.titre,
            "editeur": self.editeur, "meta": self.meta, "url": self.url,
        }


def fold(text: Optional[str]) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def title_tokens(title: Optional[str]) -> set:
    core = re.split(r"\s[-|–]\s(?=[^-|–]*$)", title or "")[0]
    return set(re.findall(r"[a-z0-9]{4,}", fold(core)))


def date_of_ref(ref: str) -> str:
    """« veille-2026-10-02 » -> « 2026-10-02 »."""
    match = _REF_DATE.search(ref)
    if not match:
        raise ValueError(f"Référence de veille invalide : {ref}")
    return "-".join(match.groups())


def french_date(ref: str) -> str:
    year, month, day = (int(x) for x in date_of_ref(ref).split("-"))
    return f"{day} {MONTHS[month - 1]} {year}"


def is_short_form(source: Dict[str, Any]) -> bool:
    """Post X: texte court par nature, et X ne se laisse pas lire de façon fiable."""
    host = urlparse(source.get("url") or "").netloc.lower().removeprefix("www.")
    return host in ("x.com", "twitter.com")


def post_text(source: Dict[str, Any], extracted: str) -> Tuple[str, bool]:
    """Texte d'une source, avec repli sur le titre relevé par Saqr pour un post X.

    Le titre d'un signal X chez Saqr EST le texte du post tel que Saqr l'a capturé. Si Dīwān n'obtient pas
    plus que ce titre (le 02/10, 71 caractères contre 228), on prend le titre: sans lui, les chiffres de la
    veille tirés de ce post n'ont plus de source et le contrôle de contenu refuse tout l'épisode.
    Le repli ne vaut QUE pour les posts X: toute autre source doit être lue par Dīwān lui-même.
    """
    title = (source.get("titre") or "").strip()
    if is_short_form(source) and len(title) > len((extracted or "").strip()):
        return title, True
    return extracted, False


def provided_text(source: Dict[str, Any]) -> str:
    """Texte intégral du signal tel que Saqr l'a capturé (`texte_complet`, livré le 03/10), ou ""."""
    raw = source.get("texte_complet")
    return raw.strip() if isinstance(raw, str) else ""


def prefer_provided(source: Dict[str, Any], text: str) -> Tuple[str, bool]:
    """Le texte de Saqr remplace ce que Diwan a lu s'il est plus complet (post X tronqué, page vide).

    Même niveau de confiance que le titre d'un post X, déjà admis en repli: c'est la capture de Saqr.
    """
    provided = provided_text(source)
    if provided and len(provided) > len((text or "").strip()):
        return provided, True
    return text, False


def source_urls(source: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Adresse à lire (celle de l'éditeur si Saqr l'a résolue) et PDF complet d'un article arXiv.

    `url_canonique` (contrat étendu du 05/10) ne passe qu'après `article_url`: elle ne sert que là où celui-ci
    est vide (mesuré: 1 source sur 3 le 04/10, 2 sur 10 le 03/10), jamais à la place d'une adresse déjà lue.
    """
    url = (source.get("article_url") or source.get("url_canonique") or source.get("url") or "").strip()
    raw = (source.get("url") or "").strip()
    match = _ARXIV_ABS.search(raw) or _ARXIV_ABS.search(url)
    return url, (f"https://arxiv.org/pdf/{match.group(1)}" if match else None)


_DISTINCT_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]{5,}")
ARXIV_MIN_TOKENS = 5
ARXIV_MIN_OVERLAP = 0.6
_ATOM = "{http://www.w3.org/2005/Atom}"


def _distinct_tokens(text: Optional[str]) -> set:
    return set(_DISTINCT_TOKEN.findall(fold(text)))


def arxiv_search_query(text: Optional[str]) -> Optional[str]:
    """Requête arXiv à partir du texte d'un post X: trois termes au plus, expressions à trait d'union d'abord.

    Mesuré le 02/10: la même requête en « OR » noie l'article parmi des travaux sans rapport; en « AND » il
    figure dans les dix premiers résultats, et pick_arxiv_match tranche.
    """
    tokens = _distinct_tokens(text)
    if len(tokens) < ARXIV_MIN_TOKENS:
        return None
    hyphenated = sorted((t for t in tokens if "-" in t), key=len, reverse=True)[:2]
    plain = sorted((t for t in tokens if "-" not in t), key=len, reverse=True)[: 3 - len(hyphenated)]
    return " AND ".join([f'all:"{t}"' for t in hyphenated] + [f"all:{t}" for t in plain])


def parse_arxiv_feed(xml_text: str) -> List[Dict[str, str]]:
    """Entrées (identifiant, titre, résumé) d'un flux Atom de l'API arXiv; flux illisible = aucune entrée."""
    # Un flux Atom n'a jamais de DTD: en refuser une écarte les entités externes et les « billion laughs ».
    if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    entries = []
    for entry in root.iter(f"{_ATOM}entry"):
        ident = (entry.findtext(f"{_ATOM}id") or "").rsplit("/abs/", 1)[-1].strip()
        if ident:
            entries.append({
                "id": ident,
                "title": (entry.findtext(f"{_ATOM}title") or "").strip(),
                "summary": (entry.findtext(f"{_ATOM}summary") or "").strip(),
            })
    return entries


def pick_arxiv_match(text: Optional[str], entries: List[Dict[str, str]]) -> Optional[str]:
    """Identifiant de l'article arXiv que le post annonce, ou None: jamais un article au hasard.

    Le post doit retrouver au moins 60 % de ses mots distinctifs dans le titre et le résumé de l'article.
    """
    wanted = _distinct_tokens(text)
    if len(wanted) < ARXIV_MIN_TOKENS:
        return None
    best, best_overlap = None, ARXIV_MIN_OVERLAP
    for entry in entries:
        overlap = len(wanted & _distinct_tokens(entry["title"] + " " + entry["summary"])) / len(wanted)
        if overlap >= best_overlap:
            best, best_overlap = entry["id"], overlap
    return best


def classify(
    source: Dict[str, Any], title: Optional[str], text: str, seen: Dict[int, str], provided: bool = False
) -> Tuple[str, str]:
    """Statut et détail d'une source à partir de ce que Diwan a lu (texte vide = lecture ratée).

    `provided`: le texte est le `texte_complet` de Saqr, pas une page lue: ni mur anti-robot ni paywall
    à y chercher, et un résumé de flux court (289 caractères le 03/10) reste un texte exploitable.
    """
    text = (text or "").strip()
    if not text:
        return ECHEC, "source non récupérée par Diwan"
    low = text[:3000].lower()
    if not provided and ("requiring captcha" in low or "verify you are human" in low):
        return ECHEC, "mur anti-robot (CAPTCHA), aucun texte d'article"
    # Un marqueur de paywall ne vaut que sur un texte court: sur un article complet,
    # « Abonnez-vous » n'est que le lien du menu.
    if not provided and len(text) < 3000 and any(m in low for m in PAYWALL_MARKERS):
        return PAYWALL, "marqueur de paywall ou d'accès refusé"
    if len(text) < (SHORT_FORM_MIN_CHARS if provided or is_short_form(source) else MIN_TEXT_CHARS):
        return ECHEC, f"texte trop court ({len(text)} caractères)"
    expected = title_tokens(source.get("titre"))
    if expected:
        haystack = set(re.findall(r"[a-z0-9]{4,}", fold((title or "") + " " + text[:1500])))
        overlap = len(expected & haystack) / len(expected)
        if overlap < 0.5:
            return DIVERGENT, f"page ne correspond pas au titre annoncé (recouvrement {overlap:.0%})"
    head = fold(text[:1200])
    for other_n, other in seen.items():
        matcher = difflib.SequenceMatcher(None, head, other, autojunk=False)
        if matcher.quick_ratio() >= 0.85 and matcher.ratio() >= 0.85:
            return REPRISE, f"quasi identique à la source {other_n}"
    return INGEREE, f"{len(text)} caractères"


def main_text(raw: Optional[str]) -> str:
    """Texte principal d'une page: sans images ni liens, sans navigation ni consentement."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    kept = []
    for line in text.splitlines():
        line = line.strip(" *•\t#>")
        if len(line) < 60 or not re.search(r"[.!?»”\"]", line):
            continue
        if len(line) < 400 and any(b in line.lower() for b in BOILERPLATE):
            continue
        kept.append(line)
    body = "\n".join(kept)
    return body or (raw or "")


def split_veille(markdown: str) -> str:
    """Corps de la veille, sans les définitions de notes de bas de page."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\[\^\d+\]:", line):
            return "\n".join(lines[:i]).rstrip()
    return markdown.rstrip()


def build_source_text(full_text: str, article_text: str = "") -> str:
    """Texte intégral d'une source: la page lue, puis l'article complet quand il est distinct."""
    base = main_text(full_text)
    article = main_text(article_text) if article_text else ""
    return base + ("\n\n[Article complet]\n" + article if article and article not in base else "")


def alteration_rate(results: List[SourceResult]) -> float:
    return sum(r.statut in ALTERED for r in results) / len(results) if results else 1.0


def build_content(ref: str, markdown: str, sources: List[Tuple[str, str]]) -> str:
    """Contenu du podcast: fil conducteur de Saqr, date, puis les sources lues DANS le notebook.

    `sources` = (titre, texte intégral) tels qu'enregistrés dans le notebook de la production.
    """
    date = french_date(ref)
    corpus = "\n\n".join(f"{title}\n{text}" for title, text in sources)
    return (
        f"Veille du {date}. Fil conducteur de l'éditeur (jamais une source) :\n"
        f"{split_veille(markdown)}\n\nDate de la veille : {date}\n\n"
        f"{SOURCES_DELIMITER}\n{corpus}\n"
    )
