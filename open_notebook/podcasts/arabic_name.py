"""Le prénom « Hanaa » dit en arabe, assemblé à la réplique française.

Demande d'Amine (03/10/2026) : aucune graphie latine ne rend la prononciation arabe de هناء. Le moteur de
voix la rend bien quand il dit « معنا هناء » en arabe (point d'entrée `/tts`, langue `ar`, sans diacritiques :
l'API OpenAI du moteur refuse tout texte arabe, HTTP 500, et `/tts` avec diacritiques aussi). Le mot « معنا »
(« avec nous ») ne doit pas s'entendre : on coupe le clip au creux d'énergie entre les deux mots, validé à
l'écoute par Amine (essai 2 de Rim, puis les cinq voix du panel), puis on assemble :
partie française, prénom arabe, partie française, dans la voix de celui qui parle.
"""

import array
import asyncio
import os
import re
import shutil
import wave
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from loguru import logger

from open_notebook.podcasts.tts_text import normalize_for_tts

NAME = re.compile(r"\bHanaa\b", re.IGNORECASE)
CARRIER = "معنا هناء"
DEFAULT_URL = "http://192.168.100.20:7861/tts"
TIMEOUT_SECONDS = 120
SAMPLE_RATE = 44100
FRAME = SAMPLE_RATE // 100
LOW_SHARE, HIGH_SHARE = 0.30, 0.60  # fenêtre du creux, en part de la durée parlée du clip
VOICED_FLOOR = 0.06  # une trame est parlée au-dessus de 6 % du pic
FADE_IN_SECONDS = 0.008
GAP_SECONDS = 0.04
MIN_FRAGMENT_CHARS = 7  # en dessous, le moteur répond HTTP 500 (voir tts_text.MIN_SENTENCE_CHARS)


def tts_url() -> str:
    return os.getenv("DIWAN_ARABIC_TTS_URL") or DEFAULT_URL


def has_arabic_name(text: str) -> bool:
    return bool(NAME.search(text))


def split_around_name(text: str) -> List[Tuple[bool, str]]:
    """(est_le_prénom, texte) dans l'ordre ; les morceaux sans lettre (ponctuation seule) sont écartés."""
    parts: List[Tuple[bool, str]] = []
    cursor = 0
    for match in NAME.finditer(text):
        before = text[cursor : match.start()]
        if re.search(r"\w", before):
            parts.append((False, before))
        parts.append((True, match.group(0)))
        cursor = match.end()
    after = text[cursor:]
    if re.search(r"\w", after):
        parts.append((False, after))
    return parts


def pad_fragment(text: str) -> str:
    """Complète un fragment trop court pour le moteur : « Merci » → « Merci ... » (le moteur l'accepte)."""
    stripped = text.strip()
    if len(stripped) >= MIN_FRAGMENT_CHARS:
        return text
    return re.sub(r"[\s,;:]+$", "", stripped) + " ..."


def fragment_candidates(piece: str) -> List[str]:
    """Écritures à essayer, dans l'ordre, pour une partie française (HTTP 500 du moteur sur certains fragments,
    ex. débutant par une virgule) : telle quelle, sans ponctuation en bordure, puis normalisée."""
    stripped = piece.strip(" ,;:")
    candidates: List[str] = []
    for text in (piece, stripped, normalize_for_tts(stripped)):
        padded = pad_fragment(text) if re.search(r"\w", text) else ""
        if padded and padded not in candidates:
            candidates.append(padded)
    return candidates


def _rms(data: array.array, start: int) -> float:
    chunk = data[start : start + FRAME]
    return (sum(x * x for x in chunk) / max(len(chunk), 1)) ** 0.5


def cut_after_carrier(data: array.array) -> array.array:
    """Garde le prénom : la fin du clip « معنا هناء », à partir du creux d'énergie entre les deux mots."""
    levels = [_rms(data, i) for i in range(0, len(data) - FRAME, FRAME)]
    if not levels:
        raise ValueError("clip arabe vide")
    voiced = [i for i, level in enumerate(levels) if level > max(levels) * VOICED_FLOOR]
    if len(voiced) < 10:
        raise ValueError("clip arabe sans parole")
    first, last = voiced[0], voiced[-1]
    low = first + int((last - first) * LOW_SHARE)
    high = first + int((last - first) * HIGH_SHARE)
    dip = min(range(low, high + 1), key=lambda i: levels[i])
    clip = data[dip * FRAME : min((last + 3) * FRAME, len(data))]
    fade = int(SAMPLE_RATE * FADE_IN_SECONDS)
    for i in range(min(fade, len(clip))):
        clip[i] = int(clip[i] * i / fade)
    return clip


def read_wav(path: Path) -> array.array:
    with wave.open(str(path)) as handle:
        return array.array("h", handle.readframes(handle.getnframes()))


def write_wav(path: Path, data: array.array) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(data.tobytes())


async def _ffmpeg(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg a échoué : {stderr.decode(errors='replace')[:200]}")


REFERENCE_DIR = Path(__file__).parent / "assets"


def reference_clip(voice: str) -> Optional[Path]:
    """Clip de référence du prénom pour cette voix, choisi à l'écoute par Amine (prise 2 de la session du
    04/10/2026) : toujours la même prononciation, sans appel au moteur. None si la voix n'en a pas."""
    path = REFERENCE_DIR / f"hanaa_{voice}.wav"
    return path if path.is_file() else None


async def fetch_name_clip(voice: str, output_wav: Path) -> None:
    """Le prénom seul, dans la voix demandée, prêt à être assemblé (WAV mono 44,1 kHz).

    Le clip de référence de la voix prime ; sans lui, le prénom est généré par `/tts` (prononciation variable
    d'une génération à l'autre : durée de 0,31 à 0,57 s mesurée le 04/10)."""
    reference = reference_clip(voice)
    if reference is not None:
        shutil.copyfile(reference, output_wav)
        return
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(
            tts_url(), json={"text": CARRIER, "voice": voice, "language": "ar", "tachkil": False}
        )
        response.raise_for_status()
    raw = output_wav.with_suffix(".raw.wav")
    raw.write_bytes(response.content)
    await _ffmpeg("-i", str(raw), "-ar", str(SAMPLE_RATE), "-ac", "1", str(output_wav))
    write_wav(output_wav, cut_after_carrier(read_wav(output_wav)))
    raw.unlink(missing_ok=True)


async def assemble(parts: List[Path], output_mp3: Path) -> None:
    """Met bout à bout les morceaux (mp3 ou wav) avec une pause de 40 ms, en un seul mp3 128 kbit/s."""
    silence = array.array("h", [0] * int(SAMPLE_RATE * GAP_SECONDS))
    joined = array.array("h")
    for index, part in enumerate(parts):
        wav = part.with_name(f"{part.stem}.assemble{index}.wav")
        await _ffmpeg("-i", str(part), "-ar", str(SAMPLE_RATE), "-ac", "1", str(wav))
        joined.extend(read_wav(wav))
        joined.extend(silence)
        wav.unlink(missing_ok=True)
    work = output_mp3.with_name(f"{output_mp3.stem}.assemble.wav")
    write_wav(work, joined)
    await _ffmpeg("-i", str(work), "-b:a", "128k", str(output_mp3))
    work.unlink(missing_ok=True)


def clean(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except OSError as exc:  # le nettoyage ne doit jamais faire échouer un clip
        logger.debug(f"[audio] nettoyage de {directory} impossible ({exc})")
