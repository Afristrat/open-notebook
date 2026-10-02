"""Vérification du vrai format d'un épisode avant d'annoncer « pret » à Saqr.

Saqr a signalé (02/10/2026) que le moteur de voix rend parfois du WAV étiqueté audio/mpeg
quand on demande du MP3. L'épisode final est assemblé par Diwan (ffmpeg), mais la règle reste:
on ne dit « pret » qu'après avoir lu le fichier, jamais d'après son extension ni son type MIME.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

MIN_DURATION_SECONDS = 60.0
_PROBE_TIMEOUT_SECONDS = 60


class AudioInvalid(ValueError):
    """Le fichier n'est pas un MP3 exploitable: l'épisode ne doit pas être annoncé comme prêt."""


def _looks_like_mp3(head: bytes) -> bool:
    # Étiquette ID3 en tête, ou synchronisation de trame MPEG (11 bits à 1).
    return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)


async def probe_mp3(path: Path) -> Dict[str, Any]:
    """Retourne {codec, duration_s} d'un vrai MP3, sinon lève AudioInvalid."""
    if not path.is_file() or path.stat().st_size == 0:
        raise AudioInvalid("Le fichier audio est absent ou vide.")
    with path.open("rb") as handle:
        head = handle.read(4)
    if not _looks_like_mp3(head):
        raise AudioInvalid("Le fichier audio n'est pas un MP3 (en-tête inattendu, WAV étiqueté MP3 ?).")

    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries", "format=format_name,duration:stream=codec_name",
        str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise AudioInvalid("La lecture du fichier audio par ffprobe a dépassé le délai.") from exc
    if process.returncode != 0:
        raise AudioInvalid("ffprobe ne sait pas lire ce fichier audio.")

    info = json.loads(stdout.decode("utf-8") or "{}")
    codecs = {s.get("codec_name") for s in info.get("streams", [])}
    format_name = (info.get("format") or {}).get("format_name") or ""
    if "mp3" not in codecs or "mp3" not in format_name:
        raise AudioInvalid(f"Le codec audio n'est pas MP3 (codecs {sorted(c for c in codecs if c)}).")
    raw_duration = (info.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else 0.0
    except (TypeError, ValueError) as exc:
        raise AudioInvalid("La durée du fichier audio est illisible.") from exc
    if duration < MIN_DURATION_SECONDS:
        raise AudioInvalid(f"L'épisode est trop court ({duration:.0f} s).")
    return {"codec": "mp3", "duration_s": round(duration, 1)}
