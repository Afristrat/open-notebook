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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from langchain_core.runnables import RunnableConfig
from loguru import logger

MARKER = "[CONTRÔLE VEILLE]"
SOURCES_DELIMITER = "=== SOURCES INGÉRÉES ==="
DATE_LINE_PREFIX = "date de la veille :"

# Termes commerciaux interdits, sauf s'ils figurent dans une source.
_FORBIDDEN = (
    ("prix", r"\bprix\b"),
    ("tarif", r"\btarif(?:s|ication)?\b"),
    ("licence", r"\blicences?\b"),
    ("appel d'offres", r"\bappels? d'offres?\b"),
    ("canal de vente", r"\bcanau(?:x|l) de vente\b"),
    ("modèle de revenus", r"\bmodeles? de revenus?\b"),
    ("go-to-market", r"\bgo[- ]to[- ]market\b"),
    ("pricing", r"\bpricing\b"),
    ("abonnement", r"\babonnements?\b"),
    ("revendeur", r"\brevendeurs?\b"),
    ("rentabilité", r"\brentabilite\b"),
    ("monétisation", r"\bmonetis\w*"),
    ("mise sur le marché", r"\bmise sur le marche\b"),
    ("stratégie commerciale", r"\bstrategie commerciale\b"),
)
_FORBIDDEN_COMPILED = tuple((label, re.compile(rx)) for label, rx in _FORBIDDEN)

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


def _canonical_numbers(text: str) -> str:
    """Rend les nombres comparables : « 150 000 » → 150000, « 23,2 » → 23.2."""
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"(?<=\d) (?=\d{3}\b)", "", text)
    return re.sub(r"(?<=\d),(?=\d)", ".", text)


def _normalize_number(token: str) -> str:
    return token.rstrip("0").rstrip(".") if "." in token else token


def _numbers(text: str) -> Dict[str, bool]:
    """Nombres du texte (forme canonique) → vrai s'ils doivent être contrôlés."""
    found: Dict[str, bool] = {}
    for match in _NUMBER.finditer(_canonical_numbers(text)):
        token = _normalize_number(match.group(1))
        has_unit = bool(match.group(2))
        checked = has_unit or "." in token or len(token) >= 2
        found[token] = found.get(token, False) or checked
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
) -> GuardReport:
    """Confronte chaque réplique au corpus des sources."""
    report = GuardReport()
    corpus_folded = _fold(corpus)
    allowed_numbers = set(_numbers(corpus)) | set(_numbers(extra_allowed))
    corpus_words = set(re.findall(r"[\w'-]+", corpus_folded))
    names = {_fold(n) for n in speaker_names}

    for index, line in enumerate(lines):
        folded = _fold(line)

        for label, pattern in _FORBIDDEN_COMPILED:
            if pattern.search(folded) and not pattern.search(corpus_folded):
                report.violations.append(
                    GuardIssue(index, "terme_commercial", f"« {label} » absent des sources")
                )

        for token, checked in _numbers(line).items():
            if checked and token not in allowed_numbers:
                report.violations.append(
                    GuardIssue(index, "nombre_absent", f"{token} absent des sources")
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
    report = check_transcript(
        [d.dialogue for d in transcript],
        corpus,
        extra_allowed=allowed_dates(state.get("content")),
        speaker_names=names,
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
