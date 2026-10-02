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
MAX_SOURCE_CHARS = 5000
EXTRACT_CHARS = 3500
MAX_ALTERATION = 0.10

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


def source_urls(source: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Adresse à lire (celle de l'éditeur si Saqr l'a résolue) et PDF complet d'un article arXiv."""
    url = (source.get("article_url") or source.get("url") or "").strip()
    raw = (source.get("url") or "").strip()
    match = _ARXIV_ABS.search(raw) or _ARXIV_ABS.search(url)
    return url, (f"https://arxiv.org/pdf/{match.group(1)}" if match else None)


def classify(
    source: Dict[str, Any], title: Optional[str], text: str, seen: Dict[int, str]
) -> Tuple[str, str]:
    """Statut et détail d'une source à partir de ce que Diwan a lu (texte vide = lecture ratée)."""
    text = (text or "").strip()
    if not text:
        return ECHEC, "source non récupérée par Diwan"
    low = text[:3000].lower()
    if "requiring captcha" in low or "verify you are human" in low:
        return ECHEC, "mur anti-robot (CAPTCHA), aucun texte d'article"
    # Un marqueur de paywall ne vaut que sur un texte court: sur un article complet,
    # « Abonnez-vous » n'est que le lien du menu.
    if len(text) < 3000 and any(m in low for m in PAYWALL_MARKERS):
        return PAYWALL, "marqueur de paywall ou d'accès refusé"
    if len(text) < (SHORT_FORM_MIN_CHARS if is_short_form(source) else MIN_TEXT_CHARS):
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


def main_text(raw: Optional[str], limit: int = MAX_SOURCE_CHARS) -> str:
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
    return (body or (raw or ""))[:limit]


def split_veille(markdown: str) -> str:
    """Corps de la veille, sans les définitions de notes de bas de page."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\[\^\d+\]:", line):
            return "\n".join(lines[:i]).rstrip()
    return markdown.rstrip()


def _number_tokens(narrative: str) -> set:
    tokens = set()
    for match in re.finditer(r"\d+[.,]\d+|\d{2,}", narrative):
        token = match.group(0)
        tokens.update({token, token.replace(",", "."), token.replace(".", ",")})
    return tokens


def article_excerpt(
    article_text: str, narrative: str, already: str = "", limit: int = EXTRACT_CHARS
) -> str:
    """Phrases de l'article qui portent les nombres cités par la veille (les plus riches d'abord)."""
    tokens = _number_tokens(narrative)
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", article_text or ""))
    scored = []
    for sentence in sentences:
        if not 40 <= len(sentence) <= 400 or sentence in already:
            continue
        hits = {
            t.replace(",", ".") for t in tokens
            if re.search(r"(?<![\d.,])" + re.escape(t) + r"(?!\d)", sentence)
        }
        if hits:
            scored.append((len(hits), -len(sentence), sentence))
    chosen, size = [], 0
    for _, _, sentence in sorted(scored, reverse=True):
        if size + len(sentence) <= limit:
            chosen.append(sentence)
            size += len(sentence)
    return "\n".join(chosen)


def build_source_text(full_text: str, narrative: str, article_text: str = "") -> str:
    """Texte transmis au modèle pour une source: début de page + extraits chiffrés de l'article complet."""
    base = main_text(full_text)
    pool = (article_text or "") + "\n" + (full_text if len(full_text or "") > len(base) else "")
    excerpt = article_excerpt(pool, narrative, already=base)
    return base + ("\n[Extraits de l'article complet]\n" + excerpt if excerpt else "")


def alteration_rate(results: List[SourceResult]) -> float:
    return sum(r.statut in ALTERED for r in results) / len(results) if results else 1.0


def build_content(ref: str, markdown: str, results: List[SourceResult]) -> str:
    """Contenu du podcast: fil conducteur de Saqr, date, puis les textes que Diwan a lus."""
    date = french_date(ref)
    corpus = "\n\n".join(
        f"[Source {r.n}] {r.titre} ({r.editeur}, {r.meta})\n{r.texte}"
        for r in results
        if r.statut in (INGEREE, REPRISE) and r.texte
    )
    return (
        f"Veille du {date}. Fil conducteur de l'éditeur (jamais une source) :\n"
        f"{split_veille(markdown)}\n\nDate de la veille : {date}\n\n"
        f"{SOURCES_DELIMITER}\n{corpus}\n"
    )
