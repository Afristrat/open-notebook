"""Compute podcast generation progress by reading on-disk artifacts.

The podcast pipeline writes, in order, inside the episode directory:
    outline.json            -> outline ready
    transcript.json         -> a JSON list of N lines (total reply count)
    clips/NNNN.mp3          -> one clip per line (done count)
    audio/<name>.mp3        -> final episode (complete)

Knowing N (from transcript.json) and the number of clips already written, the
real percentage is computable while the job is still running. The job status
from surreal-commands stays authoritative for terminal states.
"""

import json
from pathlib import Path
from typing import Optional, TypedDict
from urllib.parse import unquote, urlparse

from open_notebook.podcasts.audio_paths import resolve_contained_audio_path

# Phase weights. The TTS phase (one clip per line, serialized by the VoxCPM
# lock) is by far the longest, so it owns most of the bar (10% -> 95%).
_PERCENT_OUTLINE = 3
_PERCENT_TRANSCRIPT = 8
_PERCENT_TTS_START = 10
_PERCENT_TTS_END = 95
_PERCENT_COMBINING = 97


class ProgressInfo(TypedDict):
    status: Optional[str]
    phase: str
    done: int
    total: int
    percent: int


def _resolve_path(raw: str) -> Path:
    if raw.startswith("file://"):
        return Path(unquote(urlparse(raw).path))
    return Path(raw)


def _episode_dir(output_dir: Optional[str], audio_file: Optional[str]) -> Optional[Path]:
    """Locate the episode directory.

    Prefers the stored output_dir; falls back to deriving it from a final
    audio_file path (<dir>/audio/<name>.mp3 -> <dir>) for legacy episodes.

    Since migration 21, audio_file is stored RELATIVE to PODCASTS_FOLDER, so
    the fallback goes through the shared read-side resolver instead of
    treating the stored value as a filesystem path.
    """
    if output_dir:
        return _resolve_path(output_dir)
    if audio_file:
        audio_path = resolve_contained_audio_path(audio_file)
        if audio_path is None:
            return None
        # <episode_dir>/audio/<name>.mp3
        return audio_path.parent.parent
    return None


def _count_total(transcript_path: Path) -> int:
    try:
        data = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        inner = data.get("transcript")
        if isinstance(inner, list):
            return len(inner)
    return 0


def compute_episode_progress(
    output_dir: Optional[str],
    audio_file: Optional[str],
    job_status: Optional[str],
) -> ProgressInfo:
    """Return progress info {status, phase, done, total, percent}."""
    status = job_status or "unknown"

    if status in ("failed", "error"):
        return ProgressInfo(
            status=status, phase="failed", done=0, total=0, percent=0
        )

    base = _episode_dir(output_dir, audio_file)

    final_done = False
    if base is not None:
        audio_dir = base / "audio"
        final_done = audio_dir.is_dir() and any(audio_dir.glob("*.mp3"))

    if status == "completed" or final_done:
        return ProgressInfo(
            status="completed", phase="done", done=1, total=1, percent=100
        )

    if base is None or not base.is_dir():
        # No directory known yet (e.g. legacy in-flight episode): report queued.
        return ProgressInfo(
            status=status, phase="pending", done=0, total=0, percent=0
        )

    if not (base / "outline.json").exists():
        return ProgressInfo(
            status=status, phase="outline", done=0, total=0,
            percent=_PERCENT_OUTLINE,
        )

    transcript_path = base / "transcript.json"
    total = _count_total(transcript_path) if transcript_path.exists() else 0
    if total == 0:
        return ProgressInfo(
            status=status, phase="transcript", done=0, total=0,
            percent=_PERCENT_TRANSCRIPT,
        )

    clips_dir = base / "clips"
    done = sum(1 for _ in clips_dir.glob("*.mp3")) if clips_dir.is_dir() else 0
    done = min(done, total)

    if done >= total:
        return ProgressInfo(
            status=status, phase="combining", done=done, total=total,
            percent=_PERCENT_COMBINING,
        )

    span = _PERCENT_TTS_END - _PERCENT_TTS_START
    percent = _PERCENT_TTS_START + int(span * done / total)
    return ProgressInfo(
        status=status, phase="synthesizing", done=done, total=total,
        percent=percent,
    )
