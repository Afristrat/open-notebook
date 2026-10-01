"""Contrôle de contenu d'un podcast de veille, avant la synthèse vocale.

Principe : la veille de l'éditeur n'est qu'un fil conducteur, jamais une preuve.
Chaque chiffre, chaque terme commercial et chaque nom propre de la transcription
est confronté aux textes des sources que Diwan a lui-même récupérés. Un fait
présent dans la veille mais absent des sources fait donc échouer le contrôle,
au lieu d'être répété et amplifié.

Le contrôle ne s'active que si le briefing du profil contient `MARKER`, et il lit
le corpus des sources après `SOURCES_DELIMITER` dans le contenu du podcast.

Limites assumées, à compléter par une vérification sémantique en aval :
- un script voit un chiffre ou un terme absent, pas un sens déformé ;
- un terme commercial autorisé par une source (« licence Apache ») l'est partout ;
- les entiers à un seul chiffre sans unité ne sont pas contrôlés.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from langchain_core.runnables import RunnableConfig
from loguru import logger

# Fourchette de mots pour 8 à 12 minutes de voix : le moteur de voix tient 150 mots
# par minute sur répliques courtes et 177 sur répliques longues (mesuré le
# 2026-10-01 sur deux épisodes terminés), soit environ 1 100 à 2 000 mots.
MIN_WORDS = 1100
MAX_WORDS = 2000

# Ouverture : chaque intervenant doit avoir été nommé ET avoir pris la parole dans
# ces premières répliques (retour d'écoute d'Amine, 2026-10-01 : l'épisode entrait
# dans les faits sans présenter les profils qui débattent). 0 désactive le contrôle.
INTRO_LINES = 10

# Part de parole minimale de chaque intervenant : le profil Veille réserve à Tariq
# l'usage réel au Maroc, il ne doit pas être réduit à quelques répliques (mesuré le
# 2026-10-01 : 6 répliques sur 56, puis 16 sur 80 soit 20 %). 0 désactive.
MIN_SPEAKER_SHARE = 0.22

# Clôture : la dernière réplique est celle de l'animateur (le premier à parler), qui
# donne l'action du jour et salue. Constaté le 2026-10-01 : l'épisode se terminait sur
# une opinion de Tariq, sans salut. False désactive.
HOST_CLOSES = True

# Nombres français écrits en lettres (« quatre mille », « cent cinquante mille »).
_UNITS = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
    "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "trente": 30,
    "quarante": 40, "cinquante": 50, "soixante": 60,
}
_MULTIPLIERS = {"mille": 1000, "million": 10**6, "millions": 10**6,
                "milliard": 10**9, "milliards": 10**9}
_NUMBER_WORDS = set(_UNITS) | set(_MULTIPLIERS) | {"vingt", "vingts", "cent", "cents"}

MARKER = "[CONTRÔLE VEILLE]"
SOURCES_DELIMITER = "=== SOURCES INGÉRÉES ==="
DATE_LINE_PREFIX = "date de la veille :"

# Termes commerciaux interdits dans la transcription (français), sauf si une source
# les établit : on accepte alors leur forme française OU leur équivalent anglais,
# car les sources sont souvent en anglais (« MIT license » justifie « licence MIT »).
_FORBIDDEN = (
    ("prix", r"\bprix\b", r"\bprices?\b|\bpricing\b"),
    ("tarif", r"\btarif(?:s|ication)?\b", r"\btariffs?\b|\brate card\b"),
    ("licence", r"\blicences?\b", r"\blicen[sc](?:e|es|ed|ing)\b"),
    ("appel d'offres", r"\bappels? d'offres?\b", r"\btenders?\b|\brequests? for proposals?\b|\brfps?\b"),
    ("canal de vente", r"\bcanau(?:x|l) de vente\b", r"\b(?:sales|distribution) channels?\b|\bresellers?\b"),
    ("modèle de revenus", r"\bmodeles? de revenus?\b", r"\brevenue models?\b|\bbusiness models?\b"),
    ("go-to-market", r"\bgo[- ]to[- ]market\b", r"\bgo[- ]to[- ]market\b"),
    ("pricing", r"\bpricing\b", r"\bpricing\b|\bprices?\b"),
    ("abonnement", r"\babonnements?\b", r"\bsubscriptions?\b"),
    ("revendeur", r"\brevendeurs?\b", r"\bresellers?\b"),
    ("rentabilité", r"\brentabilite\b", r"\bprofitab\w*|\bprofit margins?\b"),
    ("monétisation", r"\bmonetis\w*", r"\bmoneti[sz]\w*"),
    ("mise sur le marché", r"\bmise sur le marche\b", r"\bgo[- ]to[- ]market\b|\bmarket entry\b"),
    ("stratégie commerciale", r"\bstrategie commerciale\b", r"\b(?:commercial|sales) strateg\w*"),
)
_FORBIDDEN_COMPILED = tuple(
    (label, re.compile(fr), re.compile(en)) for label, fr, en in _FORBIDDEN
)

_NUMBER = re.compile(r"(\d+(?:\.\d+)?)(\s*(?:%|€|\$))?")
_PROPER_NOUN = re.compile(r"(?<=[a-zàâçéèêëîïôûùüÿ,;:] )([A-ZÀÂÇÉÈÊËÎÏÔÛÙÜ][\wÀ-ÿ'-]{2,})")


@dataclass
class GuardIssue:
    line: int
    kind: str
    detail: str


@dataclass
class GuardReport:
    violations: List[GuardIssue] = field(default_factory=list)
    warnings: List[GuardIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        def dump(issues: Iterable[GuardIssue]) -> List[Dict[str, Any]]:
            return [
                {"ligne": i.line, "type": i.kind, "detail": i.detail} for i in issues
            ]

        return {
            "violations": dump(self.violations),
            "warnings": dump(self.warnings),
        }


def _fold(text: str) -> str:
    """Minuscules sans accents, apostrophes droites, espaces insécables normalisés."""
    text = text.replace(" ", " ").replace(" ", " ").replace("’", "'")
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# Échelles écrites après un chiffre : « 3 billion » (anglais) = « 3 milliards » = 3 000 000 000.
_SCALES = (
    (re.compile(r"(\d+(?:\.\d+)?)\s?(?:billion|milliards?)\b", re.I), 10**9),
    (re.compile(r"(\d+(?:\.\d+)?)\s?(?:millions?)\b", re.I), 10**6),
    (re.compile(r"(\d+(?:\.\d+)?)\s?(?:thousand|mille)\b", re.I), 1000),
)


def _canonical_numbers(
    text: str, comma_is_thousands: bool, expand_scales: bool = False
) -> str:
    """Rend les nombres comparables : « 150 000 », « 150,000 » et « 150k » → 150000.

    La virgule est ambiguë : séparateur de milliers en anglais (« 2,000 »), décimale
    en français (« 23,2 »). Le texte des sources est lu sous les deux lectures.
    """
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"(?<=\d) (?=\d{3}\b)", "", text)
    if comma_is_thousands:
        text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    else:
        text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = re.sub(
        r"(\d+(?:\.\d+)?)\s?[kK]\b",
        lambda match: str(int(float(match.group(1)) * 1000)),
        text,
    )
    if expand_scales:
        for pattern, factor in _SCALES:
            text = pattern.sub(partial(_scaled, factor=factor), text)
    return text


def _scaled(match: "re.Match[str]", factor: int) -> str:
    return str(int(float(match.group(1)) * factor))


def _normalize_number(token: str) -> str:
    return token.rstrip("0").rstrip(".") if "." in token else token


def _numbers(
    text: str, comma_is_thousands: bool = False, expand_scales: bool = False
) -> Dict[str, bool]:
    """Nombres du texte (forme canonique) → vrai s'ils doivent être contrôlés."""
    found: Dict[str, bool] = {}
    canonical = _canonical_numbers(text, comma_is_thousands, expand_scales)
    for match in _NUMBER.finditer(canonical):
        token = _normalize_number(match.group(1))
        has_unit = bool(match.group(2))
        checked = has_unit or "." in token or len(token) >= 2
        found[token] = found.get(token, False) or checked
    return found


def french_numbers(text: str) -> List[Tuple[int, bool]]:
    """Nombres écrits en lettres → (valeur, suivi de « pour cent »).

    « quatre mille » → 4000, « quatre-vingt-seize » → 96, « deux mille vingt-six »
    → 2026, « vingt pour cent » → (20, True).
    """
    tokens = re.findall(r"[a-z]+|\d+", _fold(text))
    found: List[Tuple[int, bool]] = []
    i = 0
    while i < len(tokens):
        if tokens[i] not in _NUMBER_WORDS:
            i += 1
            continue
        # Un mot d'échelle collé à un chiffre (« 3 milliards », « 5 pour cent ») est
        # déjà lu par la lecture des chiffres : on ne le compte pas une seconde fois.
        after_digit = i > 0 and tokens[i - 1].isdigit()
        pour_cent = tokens[i] in ("cent", "cents") and tokens[i - 1 : i] == ["pour"]
        if after_digit or pour_cent:
            while i < len(tokens) and tokens[i] in _NUMBER_WORDS:
                i += 1
            continue
        total = current = 0
        while i < len(tokens):
            word = tokens[i]
            if word in _UNITS:
                current += _UNITS[word]
            elif word in ("vingt", "vingts"):
                current = current * 20 if current == 4 else current + 20
            elif word in ("cent", "cents"):
                current = max(current, 1) * 100
            elif word in _MULTIPLIERS:
                total += max(current, 1) * _MULTIPLIERS[word]
                current = 0
            elif (
                word == "et"
                and current % 10 == 0
                and current > 0
                and i + 1 < len(tokens)
                and tokens[i + 1] in ("un", "onze")
            ):
                pass  # « vingt et un », « soixante et onze »
            else:
                break
            i += 1
        percent = tokens[i : i + 2] == ["pour", "cent"]
        if percent:
            i += 2
        found.append((total + current, percent))
    return found


def extract_corpus(content: Any) -> str:
    """Texte des sources, après le séparateur ; lève ValueError s'il est introuvable."""
    text = "\n".join(content) if isinstance(content, (list, tuple)) else str(content or "")
    if SOURCES_DELIMITER not in text:
        raise ValueError(
            "Contrôle de contenu impossible : le corpus des sources est introuvable "
            f"(séparateur « {SOURCES_DELIMITER} » absent du contenu)"
        )
    return text.split(SOURCES_DELIMITER, 1)[1]


def allowed_dates(content: Any) -> str:
    """Nombres d'une ligne « Date de la veille : … » (la date d'ouverture est légitime)."""
    text = "\n".join(content) if isinstance(content, (list, tuple)) else str(content or "")
    lines = [
        line for line in text.splitlines()
        if line.strip().lower().startswith(DATE_LINE_PREFIX)
    ]
    return " ".join(lines)


def check_transcript(
    lines: Sequence[str],
    corpus: str,
    extra_allowed: str = "",
    speaker_names: Iterable[str] = (),
    word_range: Optional[Sequence[int]] = None,
    speakers: Optional[Sequence[str]] = None,
    intro_lines: int = 0,
    min_share: float = 0.0,
    closing_speaker: Optional[str] = None,
) -> GuardReport:
    """Confronte chaque réplique au corpus des sources, la longueur à la fourchette,
    l'ouverture à la présentation des intervenants, leur part de parole et la clôture."""
    report = GuardReport()
    if closing_speaker and speakers and _fold(speakers[-1]) != _fold(closing_speaker):
        report.violations.append(
            GuardIssue(
                len(speakers) - 1,
                "cloture_absente",
                f"la dernière réplique doit être celle de {closing_speaker} "
                f"(action du jour et salut), elle est de {speakers[-1]}",
            )
        )
    if min_share and speakers and speaker_names:
        folded_speakers = [_fold(s) for s in speakers]
        for name in speaker_names:
            count = folded_speakers.count(_fold(name))
            share = count / len(folded_speakers)
            if share < min_share:
                report.violations.append(
                    GuardIssue(
                        0,
                        "part_intervenant",
                        f"{name} ne dit que {count} réplique(s) sur "
                        f"{len(folded_speakers)} ({share:.0%}), minimum {min_share:.0%}",
                    )
                )
    if intro_lines and speakers is not None:
        opening = _fold(" ".join(lines[:intro_lines]))
        spoke = {_fold(s) for s in speakers[:intro_lines]}
        for name in speaker_names:
            folded = _fold(name)
            if not re.search(rf"\b{re.escape(folded)}\b", opening) or folded not in spoke:
                report.violations.append(
                    GuardIssue(
                        0,
                        "presentation_absente",
                        f"{name} n'est pas présenté et n'a pas pris la parole "
                        f"dans les {intro_lines} premières répliques",
                    )
                )
    if word_range is not None:
        total = sum(len(line.split()) for line in lines)
        low, high = word_range
        if not low <= total <= high:
            report.violations.append(
                GuardIssue(
                    0,
                    "duree_hors_cible",
                    f"{total} mots (cible {low} à {high}, soit 8 à 12 minutes de voix)",
                )
            )
    corpus_folded = _fold(corpus)
    allowed_numbers = set(_numbers(extra_allowed))
    for comma_is_thousands in (True, False):
        for expand_scales in (True, False):
            allowed_numbers |= set(_numbers(corpus, comma_is_thousands, expand_scales))
    corpus_words = set(re.findall(r"[\w'-]+", corpus_folded))
    names = {_fold(n) for n in speaker_names}

    for index, line in enumerate(lines):
        folded = _fold(line)

        for label, pattern, english in _FORBIDDEN_COMPILED:
            if (
                pattern.search(folded)
                and not pattern.search(corpus_folded)
                and not english.search(corpus_folded)
            ):
                report.violations.append(
                    GuardIssue(index, "terme_commercial", f"« {label} » absent des sources")
                )

        for token, checked in _numbers(line, False, True).items():
            if checked and token not in allowed_numbers:
                report.violations.append(
                    GuardIssue(index, "nombre_absent", f"{token} absent des sources")
                )

        # Nombres écrits en lettres : un montant, un millésime ou un pourcentage est un
        # fait (écart) ; une durée ou un petit nombre de conversation n'est qu'un signal.
        for value, percent in french_numbers(line):
            token = str(value)
            if token in allowed_numbers:
                continue
            if value >= 100 or percent:
                report.violations.append(
                    GuardIssue(
                        index, "nombre_absent", f"{token} (écrit en lettres) absent des sources"
                    )
                )
            elif value >= 10:
                report.warnings.append(
                    GuardIssue(index, "nombre_en_lettres_absent", f"{token} absent des sources")
                )

        for noun in _PROPER_NOUN.findall(line):
            word = _fold(noun)
            if word not in corpus_words and word not in names:
                report.warnings.append(
                    GuardIssue(index, "nom_propre_absent", f"{noun} absent des sources")
                )
    return report


async def content_guard_node(
    state: Any, config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """Nœud LangGraph : échoue avant la synthèse vocale si la transcription s'écarte des sources."""
    if MARKER not in (state.get("briefing") or ""):
        return {}

    transcript = state.get("transcript") or []
    corpus = extract_corpus(state.get("content"))
    profile = state.get("speaker_profile")
    names = [s.name for s in getattr(profile, "speakers", [])]
    speakers_in_order = [getattr(d, "speaker", "") for d in transcript]
    report = check_transcript(
        [d.dialogue for d in transcript],
        corpus,
        extra_allowed=allowed_dates(state.get("content")),
        speaker_names=names,
        word_range=(MIN_WORDS, MAX_WORDS),
        speakers=speakers_in_order,
        intro_lines=INTRO_LINES,
        min_share=MIN_SPEAKER_SHARE,
        closing_speaker=speakers_in_order[0] if HOST_CLOSES and speakers_in_order else None,
    )

    output_dir = state.get("output_dir")
    if output_dir:
        try:
            (Path(output_dir) / "guard_report.json").write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"[guard] rapport non écrit : {exc}")

    if report.violations:
        shown = "; ".join(
            f"ligne {v.line} ({v.kind}) {v.detail}" for v in report.violations[:8]
        )
        raise ValueError(
            f"Contrôle de contenu : {len(report.violations)} écart(s) avec les sources, "
            f"aucune voix générée. {shown}"
        )
    logger.info(
        f"[guard] transcription conforme aux sources "
        f"({len(report.warnings)} avertissement(s) sur des noms propres)"
    )
    return {}
