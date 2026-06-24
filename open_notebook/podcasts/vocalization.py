r"""Per-line Arabic vocalization (tashkil) for podcast transcripts.

Observed issue: when the LLM vocalizes a whole Arabic episode in a single pass
(tashkil directive embedded in the briefing), tashkil quality degrades past a
dozen lines. Yet the TTS engine (VoxCPM2, MSA) needs reliable tashkil to
pronounce correctly.

Root-cause fix: between transcript generation and audio synthesis we insert a
LangGraph node that re-vocalizes EACH line individually. A single line is short,
so its tashkil stays reliable. The node first strips any residual tashkil, then
asks for a clean full vocalization.

We extend podcast-creator's graph by replacing its module-level `graph` object
(the one create_podcast uses) with an identical copy plus the
`vocalize_transcript` node. We therefore do NOT duplicate create_podcast: all
parameter resolution and file writing stay in the library, and transcript.json
will reflect the vocalized lines.

The node is a no-op for non-Arabic episodes.

NB: no literal Arabic letters in this file (only \u escapes) -- writing Arabic
into a .py corrupts the encoding on Windows.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Dict, List

from esperanto import AIFactory
from langchain_core.runnables import RunnableConfig
from loguru import logger

# Arabic diacritics (tashkil + marks) stripped before re-vocalization so we
# start from clean text: U+0610-U+061A, U+064B-U+065F, U+0670, U+06D6-U+06DC,
# U+06DF-U+06E4, U+06E7, U+06E8, U+06EA-U+06ED.
_DIAC_RANGES = [
    (0x0610, 0x061A), (0x064B, 0x065F), (0x0670, 0x0670),
    (0x06D6, 0x06DC), (0x06DF, 0x06E4), (0x06E7, 0x06E8), (0x06EA, 0x06ED),
]
_ARABIC_DIACRITICS = re.compile(
    "[" + "".join(chr(a) + "-" + chr(b) for a, b in _DIAC_RANGES) + "]"
)

# Arabic script range (U+0600-U+06FF), used to confirm an LLM reply is Arabic.
_ARABIC_LETTERS = re.compile("[" + chr(0x0600) + "-" + chr(0x06FF) + "]")

# Instruction kept in plain ASCII French: Opus vocalizes on command, and we
# avoid any literal Arabic in the source (Windows encoding corruption).
_VOCALIZE_INSTRUCTION = (
    "Tu es un expert de l'arabe standard moderne (MSA, fus-ha). "
    "On te donne UNE replique de dialogue en arabe. "
    "Renvoie EXACTEMENT le meme texte, mot pour mot, mais ENTIEREMENT vocalise "
    "(tachkil complet): place toutes les voyelles breves (fatha, damma, kasra), "
    "le soukoun, la chadda et le tanwin sur CHAQUE mot, selon la grammaire "
    "correcte. Regles strictes: ne change aucun mot, n'ajoute ni ne retire "
    "rien, ne traduis pas, ne mets ni guillemets ni note ni explication. "
    "Reponds uniquement par le texte arabe vocalise."
)

# Number of lines vocalized in parallel (concurrent LLM calls).
_VOCALIZE_BATCH_SIZE = 5

_installed = False


def _is_arabic(language) -> bool:
    """True when the episode's resolved language is Arabic (e.g. 'Arabic')."""
    if not language:
        return False
    return "arab" in str(language).lower()


def _strip_tashkil(text: str) -> str:
    return _ARABIC_DIACRITICS.sub("", text)


def _write_progress_snapshot(state, transcript_list) -> None:
    """Write outline.json + transcript.json early, for progress tracking.

    podcast-creator only writes these at the very end of create_podcast, so the
    total line count is unknown during the long TTS phase. We snapshot them here
    (between transcript generation and audio) for every language. The library
    rewrites identical files at the end -- no conflict. Best-effort only.
    """
    output_dir = state.get("output_dir")
    if not output_dir:
        return
    try:
        base = Path(output_dir)
        base.mkdir(parents=True, exist_ok=True)
        outline = state.get("outline")
        if outline is not None and hasattr(outline, "model_dump_json"):
            (base / "outline.json").write_text(
                outline.model_dump_json(), encoding="utf-8"
            )
        (base / "transcript.json").write_text(
            json.dumps(
                [d.model_dump() for d in transcript_list],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 - snapshot is best-effort (progress only)
        logger.warning(f"[vocalize] progress snapshot failed: {e}")


async def vocalize_transcript_node(state, config: RunnableConfig) -> Dict:
    """LangGraph node: snapshot artifacts for progress + vocalize Arabic lines.

    Always writes an early outline.json/transcript.json snapshot so generation
    progress can be computed during the TTS phase. For Arabic episodes, also
    re-vocalizes each line individually (short text = reliable tashkil). If a
    line fails, its original is kept (zero data loss). Returns
    {"transcript": ...} only when the transcript was actually changed.
    """
    language = state.get("language")
    transcript = state.get("transcript") or []
    if not _is_arabic(language) or not transcript:
        _write_progress_snapshot(state, transcript)
        return {}

    configurable = config.get("configurable", {}) if config else {}
    provider = configurable.get("transcript_provider")
    model = configurable.get("transcript_model")
    extra = dict(configurable.get("transcript_config") or {})
    if not provider or not model:
        logger.warning(
            "[vocalize] missing transcript provider/model -> skipping vocalization"
        )
        _write_progress_snapshot(state, transcript)
        return {}

    # Same model as transcript generation, but plain-text output: drop any
    # JSON-structuring constraint.
    extra.pop("structured", None)
    merged_config = {"max_tokens": 2000, **extra}
    llm = AIFactory.create_language(
        provider, model, config=merged_config
    ).to_langchain()

    # Local imports: the library must exist at runtime, not at import time.
    from podcast_creator.core import (
        Dialogue,
        clean_thinking_content,
        extract_text_content,
    )

    async def _vocalize_one(idx: int, dlg) -> "Dialogue":
        raw = _strip_tashkil(dlg.dialogue).strip()
        if not raw:
            return dlg
        prompt = f"{_VOCALIZE_INSTRUCTION}\n\nReplique:\n{raw}"
        try:
            result = await llm.ainvoke(prompt)
            text = clean_thinking_content(
                extract_text_content(result.content)
            ).strip()
            if text and _ARABIC_LETTERS.search(text):
                return Dialogue(speaker=dlg.speaker, dialogue=text)
            logger.warning(
                f"[vocalize] line {idx}: empty/non-Arabic reply, keeping original"
            )
        except Exception as e:  # noqa: BLE001 - degrade without losing the line
            logger.warning(f"[vocalize] line {idx} failed: {e}; keeping original")
        return dlg

    logger.info(
        f"[vocalize] vocalizing {len(transcript)} Arabic lines, "
        f"batches of {_VOCALIZE_BATCH_SIZE}"
    )
    out: List = [None] * len(transcript)
    for start in range(0, len(transcript), _VOCALIZE_BATCH_SIZE):
        indices = list(
            range(start, min(start + _VOCALIZE_BATCH_SIZE, len(transcript)))
        )
        results = await asyncio.gather(
            *[_vocalize_one(i, transcript[i]) for i in indices]
        )
        for i, dlg in zip(indices, results):
            out[i] = dlg
    logger.info("[vocalize] vocalization done")
    _write_progress_snapshot(state, out)
    return {"transcript": out}


def ensure_vocalization_installed() -> bool:
    """Insert `vocalize_transcript` into podcast-creator's graph.

    Idempotent. Replaces the module-level `graph` object of
    podcast_creator.graph (the one create_podcast uses) with an identical copy
    augmented by the vocalization node, placed between `generate_transcript` and
    `generate_all_audio`.

    Returns True when the node is in place, False if installation failed (in
    which case the library's standard pipeline stays active).
    """
    global _installed
    if _installed:
        return True
    try:
        import podcast_creator.graph as pcg
        from langgraph.graph import END, START, StateGraph
        from podcast_creator.nodes import (
            combine_audio_node,
            generate_all_audio_node,
            generate_outline_node,
            generate_transcript_node,
            route_audio_generation,
        )
        from podcast_creator.state import PodcastState

        workflow = StateGraph(PodcastState)
        workflow.add_node("generate_outline", generate_outline_node)
        workflow.add_node("generate_transcript", generate_transcript_node)
        workflow.add_node("vocalize_transcript", vocalize_transcript_node)
        workflow.add_node("generate_all_audio", generate_all_audio_node)
        workflow.add_node("combine_audio", combine_audio_node)

        workflow.add_edge(START, "generate_outline")
        workflow.add_edge("generate_outline", "generate_transcript")
        workflow.add_edge("generate_transcript", "vocalize_transcript")
        workflow.add_conditional_edges(
            "vocalize_transcript", route_audio_generation, ["generate_all_audio"]
        )
        workflow.add_edge("generate_all_audio", "combine_audio")
        workflow.add_edge("combine_audio", END)

        pcg.graph = workflow.compile()
        _installed = True
        logger.info(
            "[vocalize] Arabic vocalization node installed into podcast graph"
        )
        return True
    except Exception as e:  # noqa: BLE001 - keep the standard pipeline
        logger.error(
            f"[vocalize] install failed, keeping standard pipeline: {e}"
        )
        return False
