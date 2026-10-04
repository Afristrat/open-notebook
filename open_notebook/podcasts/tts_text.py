"""Normalisation du texte envoyé au moteur de voix.

Constat mesuré le 2026-10-01 sur le moteur de voix de production (appels
séquentiels depuis le conteneur Diwan, mêmes identifiants que les jobs) : une
phrase de moins de 7 caractères (« Oui. », « Non. », « Hmm. ») provoque un
HTTP 500 systématique, y compris quand elle ouvre ou ponctue une réplique
longue (« Oui. L'humain reste décideur. » : 0 réussite sur 6 ; la même phrase
avec une virgule : 6 sur 6). À partir de 7 caractères, les phrases passent.

La correction est donc déterministe et se fait en amont : fusionner les phrases
trop courtes avec leur voisine, au lieu de réessayer le même texte.
"""

import re
from typing import List

# Seuil mesuré : « Ah oui. » (7 caractères) passe, « Oui. » (4) échoue.
MIN_SENTENCE_CHARS = 7

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TERMINAL_PUNCTUATION = re.compile(r"\s*[.!?…]+\s*$")

# Abréviations courtes légitimes : les fusionner changerait le sens (« M, Dupont »).
_ABBREVIATIONS = frozenset(
    {"m.", "mme.", "mlle.", "dr.", "pr.", "st.", "ste.", "me.", "mr.", "cie.", "etc."}
)


def _is_too_short(sentence: str) -> bool:
    return (
        len(sentence) < MIN_SENTENCE_CHARS
        and sentence.lower() not in _ABBREVIATIONS
    )


def _soften(sentence: str) -> str:
    """Remplace la ponctuation finale par une virgule (« Oui. » devient « Oui, »)."""
    return _TERMINAL_PUNCTUATION.sub(",", sentence)


def _lower_first(sentence: str) -> str:
    return sentence[:1].lower() + sentence[1:]


def normalize_for_tts(text: str) -> str:
    """Fusionne chaque phrase trop courte avec la phrase suivante (ou précédente).

    Le texte d'une réplique d'une seule phrase est rendu tel quel : c'est
    `tts_text_variants` qui s'en occupe.
    """
    sentences = _SENTENCE_SPLIT.split(text.strip())
    if len(sentences) < 2:
        return text

    merged: List[str] = []
    prefix = ""
    last = len(sentences) - 1
    for position, raw in enumerate(sentences):
        sentence = f"{prefix} {raw}".strip() if prefix else raw
        prefix = ""
        if _is_too_short(sentence):
            if position < last:
                prefix = _soften(sentence)
                continue
            if merged:
                merged[-1] = f"{_soften(merged[-1])} {_lower_first(sentence)}"
                continue
        merged.append(sentence)
    return " ".join(merged) if merged else text


def pad_short(text: str) -> str:
    """Dernier recours pour une réplique entière trop courte : « Oui. » → « Ah oui. »."""
    stripped = text.strip()
    if len(stripped) >= MIN_SENTENCE_CHARS:
        return text
    word = re.sub(r"[\s.!?…,;:]+$", "", stripped)
    if not word:
        return text
    tail = re.sub(r"\s+", "", stripped[len(word) :]) or "."
    return f"Ah {_lower_first(word)}{tail}"


# Lexique de prononciation : écriture envoyée au moteur de voix, jamais au texte stocké.
# Mesuré le 2026-10-01 sur le moteur de production (3 voix, transcription Whisper) :
# « arXiv » est lu « archive » par les trois voix (22 lectures sur 24), et « arxive »,
# « arkive », « ar-xive », « ArXiv » aussi ; « arksive » donne le son /ks/ attendu
# (12 sur 12, transcrit « arcsive » ou « arctive »).
# « Mehdi » : le moteur avale parfois le « é » (« Mdi ») dans un épisode ; « Médi » a été écouté et
# validé par Amine (03/10/2026, parfait dans toutes les graphies essayées).
_PRONUNCIATION_LEXICON = (
    (re.compile(r"\barxiv\b", re.IGNORECASE), "arksive"),
    (re.compile(r"\bmehdi\b", re.IGNORECASE), "Médi"),
    # « DIA » (Defense Intelligence Agency, veille du 04/10/2026) : la voix le lisait comme « d'IA »
    # (« plateforme DIA d'entreprise » pour « plateforme d'IA d'entreprise ») ; les lettres épelées
    # « D I A » ont été écoutées et choisies par Amine (essai 2). Majuscules seulement, mot entier.
    (re.compile(r"\bDIA\b"), "D I A"),
    # Grades américains abrégés (veille du 04/10/2026 : « Maj. Gen. Robert Kinney » lu « Maje, gêne »).
    (re.compile(r"\bMaj\.\s*Gen\."), "major général"),
    (re.compile(r"\bLt\.\s*Gen\."), "lieutenant général"),
    (re.compile(r"\bBrig\.\s*Gen\."), "général de brigade"),
    (re.compile(r"\bGen\.(?=\s+[A-Z])"), "général"),
)


def apply_lexicon(text: str) -> str:
    """Remplace chaque terme du lexique (mot entier, insensible à la casse) par sa graphie sonore."""
    for pattern, spoken in _PRONUNCIATION_LEXICON:
        text = pattern.sub(spoken, text)
    return text


def has_lexicon_term(text: str) -> bool:
    """Vrai si le texte contient un terme dont l'écriture est corrigée pour le moteur."""
    return apply_lexicon(text) != text


def tts_text_variants(text: str) -> List[str]:
    """Textes à essayer, dans l'ordre, pour une même réplique (sans doublon)."""
    text = apply_lexicon(text)
    normalized = normalize_for_tts(text)
    variants: List[str] = []
    for candidate in (normalized, text, pad_short(normalized)):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants
