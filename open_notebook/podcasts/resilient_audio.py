"""Génération audio résiliente pour podcast-creator : reprise au clip.

Pourquoi ce nœud remplace celui de la librairie (`generate_all_audio_node`) :
- la librairie regénère tous les clips à chaque exécution et abandonne un clip
  après 3 essais en une quinzaine de secondes ;
- un clip qui échoue y fait échouer son lot, et le job entier ;
- mesuré le 2026-10-01 : les HTTP 500 du moteur de voix sont déterministes
  (phrase de moins de 7 caractères, voir `tts_text.py`), donc réessayer le même
  texte plus longtemps ne sert à rien : il faut en changer.

Comportement :
- un clip déjà présent et non vide est repris tel quel (reprise d'un job) ;
- chaque clip est tenté avec plusieurs écritures du texte (`tts_text_variants`),
  puis avec une attente progressive pour les pannes passagères ;
- les clips qui échouent n'interrompent pas les autres : ils sont listés dans
  l'erreur finale, les clips produits restent sur disque pour la reprise.

Réglages par variables d'environnement (valeurs par défaut entre parenthèses) :
DIWAN_TTS_MAX_ATTEMPTS (8), DIWAN_TTS_WAIT_BASE (4 s), DIWAN_TTS_WAIT_MAX (90 s),
DIWAN_TTS_MAX_FAILED_CLIPS (5), et TTS_BATCH_SIZE (5) déjà lu par la librairie.
"""

import asyncio
import json
import os
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from esperanto import AIFactory
from langchain_core.runnables import RunnableConfig
from loguru import logger
from podcast_creator.core import Dialogue
from podcast_creator.nodes import combine_audio_node, generate_single_audio_clip

from open_notebook.podcasts.tts_text import has_lexicon_term, tts_text_variants
from open_notebook.podcasts.voice_treatments import (
    alters_clip,
    apply_audio_filter,
    respell,
    treatment_for,
)

# En dessous, le fichier est un déchet d'échec (en-tête sans audio), pas un clip.
MIN_CLIP_BYTES = 1000

_NON_RETRYABLE = (ValueError, TypeError, KeyError, FileNotFoundError, AssertionError)


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"[audio] {name} illisible, valeur par défaut {default}")
        return default
    return value if value >= 0 else default


def _clip_path(output_dir: Any, index: int) -> Path:
    return Path(output_dir) / "clips" / f"{index:04d}.mp3"


def _is_valid_clip(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= MIN_CLIP_BYTES


def _lexicon_marker(output_dir: Any, index: int) -> Path:
    """Marqueur « clip produit avec le lexique de prononciation et les réglages de sa voix »,
    hors du dossier des clips."""
    return Path(output_dir) / "lexicon_checked" / f"{index:04d}"


async def _generate_clip_with_language(info: Dict[str, Any], language: str) -> Path:
    """Même appel que `generate_single_audio_clip` de la librairie, plus `language` dans la requête.

    La librairie ne transmet jamais `language` (son `tts_config` va au constructeur, pas à
    `agenerate_speech`) ; or la version validée de Rim et de Younes l'exige (voir `voice_treatments`).
    """
    dialogue = info["dialogue"]
    clip_path = _clip_path(info["output_dir"], info["index"])
    clip_path.parent.mkdir(exist_ok=True, parents=True)
    tts_config = dict(info.get("tts_config") or {})
    api_key = tts_config.pop("api_key", None)
    base_url = tts_config.pop("base_url", None)
    model = AIFactory.create_text_to_speech(
        info["tts_provider"],
        info["tts_model"],
        api_key=api_key,
        base_url=base_url,
        **tts_config,
    )
    await model.agenerate_speech(
        text=dialogue.dialogue,
        voice=info["voices"][dialogue.speaker],
        output_file=clip_path,
        language=language,
    )
    return clip_path


# Seuil mesuré le 2026-10-01 : le moteur de voix produit par intermittence un clip
# avec un blanc interne de 13 à 80 secondes (6 clips sur 108 dans un épisode, à
# l'origine comme à la reprise ; le même texte régénéré est normal). Les pauses
# naturelles restent sous 1,5 s.
_SILENCE_FLOOR_DB = -45
_SILENCE_DURATION_RE = re.compile(r"silence_duration: ([\d.]+)")


async def _longest_silence(path: Path) -> float:
    """Plus long silence interne d'un clip, en secondes ; 0 s'il n'y en a pas de long.

    Si ffmpeg est indisponible, la détection est ignorée (le clip est gardé).
    """
    minimum = _env_number("DIWAN_TTS_MAX_SILENCE", 1.5)
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={_SILENCE_FLOOR_DB}dB:d={minimum}",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        logger.warning(f"[audio] détection de silence indisponible : {exc}")
        return 0.0
    found = [
        float(value)
        for value in _SILENCE_DURATION_RE.findall(stderr.decode("utf-8", "replace"))
    ]
    return max(found, default=0.0)


def _is_retryable(exc: BaseException) -> bool:
    """Même règle que la librairie : jamais les erreurs de programmation ni les 4xx (sauf 429)."""
    if isinstance(exc, _NON_RETRYABLE):
        return False
    status = getattr(exc, "status_code", None)
    return not (status is not None and 400 <= status < 500 and status != 429)


def _wait_seconds(failed_attempts: int, variants: int, base: float, cap: float) -> float:
    """Pause courte tant qu'il reste une écriture du texte à essayer, puis progressive."""
    if failed_attempts < variants:
        return 1.0
    delay = min(cap, base * 2 ** (failed_attempts - variants))
    return delay * random.uniform(0.75, 1.25)


async def synthesize_clip_resilient(dialogue_info: Dict[str, Any]) -> Path:
    """Produit un clip, ou le reprend s'il existe déjà. Lève si tous les essais échouent."""
    dialogue = dialogue_info["dialogue"]
    index = dialogue_info["index"]
    clip_path = _clip_path(dialogue_info["output_dir"], index)

    marker = _lexicon_marker(dialogue_info["output_dir"], index)
    treatment = treatment_for((dialogue_info.get("voices") or {}).get(dialogue.speaker))
    needs_lexicon = has_lexicon_term(dialogue.dialogue) or alters_clip(treatment)

    if _is_valid_clip(clip_path):
        silence = await _longest_silence(clip_path)
        if needs_lexicon and not marker.exists():
            logger.warning(
                f"[audio] clip {index:04d} existant écarté : produit avant le lexique "
                "de prononciation, il est régénéré"
            )
            clip_path.unlink(missing_ok=True)
        elif not silence:
            logger.info(f"[audio] clip {index:04d} déjà produit, repris tel quel")
            return clip_path
        else:
            logger.warning(
                f"[audio] clip {index:04d} existant écarté : silence interne de "
                f"{silence:.0f}s, il est régénéré"
            )
            clip_path.unlink(missing_ok=True)

    variants = [respell(text, treatment) for text in tts_text_variants(dialogue.dialogue)]
    max_attempts = int(_env_number("DIWAN_TTS_MAX_ATTEMPTS", 8))
    wait_base = _env_number("DIWAN_TTS_WAIT_BASE", 4)
    wait_cap = _env_number("DIWAN_TTS_WAIT_MAX", 90)

    last_error: Optional[BaseException] = None
    for attempt in range(max_attempts):
        text = variants[attempt % len(variants)]
        try:
            info = {
                **dialogue_info,
                "dialogue": Dialogue(speaker=dialogue.speaker, dialogue=text),
            }
            if treatment.language:
                path = await _generate_clip_with_language(info, treatment.language)
            else:
                path = await generate_single_audio_clip(info)
            if not _is_valid_clip(path):
                raise RuntimeError("clip vide ou tronqué")
            silence = await _longest_silence(path)
            if silence:
                raise RuntimeError(f"silence interne de {silence:.0f}s dans le clip")
            await apply_audio_filter(path, treatment)
            if attempt:
                logger.info(
                    f"[audio] clip {index:04d} produit à l'essai {attempt + 1} "
                    f"(écriture {attempt % len(variants) + 1}/{len(variants)})"
                )
            if needs_lexicon:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            return path
        except Exception as exc:  # noqa: BLE001 - classé retryable ou non juste dessous
            last_error = exc
            clip_path.unlink(missing_ok=True)
            if not _is_retryable(exc):
                raise
            wait = _wait_seconds(attempt + 1, len(variants), wait_base, wait_cap)
            logger.warning(
                f"[audio] clip {index:04d}, essai {attempt + 1}/{max_attempts} "
                f"en échec ({str(exc)[:120]}), nouvel essai dans {wait:.0f}s"
            )
            if attempt + 1 < max_attempts:
                await asyncio.sleep(wait)

    raise RuntimeError(
        f"clip {index:04d} : {max_attempts} essais épuisés ({last_error})"
    ) from last_error


async def resilient_generate_all_audio_node(
    state: Any, config: Optional[RunnableConfig] = None
) -> Dict[str, Any]:
    """Nœud LangGraph : tous les clips, par lots, sans perdre les clips déjà produits."""
    transcript = state["transcript"]
    output_dir = state["output_dir"]
    speaker_profile = state.get("speaker_profile")
    if speaker_profile is None:
        raise ValueError("speaker_profile must be provided")

    batch_size = max(1, int(_env_number("TTS_BATCH_SIZE", 5)))
    max_failed = int(_env_number("DIWAN_TTS_MAX_FAILED_CLIPS", 5))
    voices = speaker_profile.get_voice_mapping()
    default_config = speaker_profile.tts_config or {}

    def clip_info(index: int) -> Dict[str, Any]:
        speaker = speaker_profile.get_speaker_by_name(transcript[index].speaker)
        return {
            "dialogue": transcript[index],
            "index": index,
            "output_dir": output_dir,
            "tts_provider": speaker.tts_provider or speaker_profile.tts_provider,
            "tts_model": speaker.tts_model or speaker_profile.tts_model,
            "voices": voices,
            "tts_config": (
                speaker.tts_config
                if speaker.tts_config is not None
                else default_config
            ),
        }

    total = len(transcript)
    logger.info(f"[audio] {total} clips, lots de {batch_size}")
    clips: List[Optional[Path]] = [None] * total
    failures: List[str] = []

    for start in range(0, total, batch_size):
        indices = range(start, min(start + batch_size, total))
        all_reused = all(_is_valid_clip(_clip_path(output_dir, i)) for i in indices)
        results = await asyncio.gather(
            *[synthesize_clip_resilient(clip_info(i)) for i in indices],
            return_exceptions=True,
        )
        for index, result in zip(indices, results):
            if isinstance(result, BaseException):
                failures.append(f"{index:04d}: {str(result)[:160]}")
            else:
                clips[index] = result
        if len(failures) > max_failed:
            break
        # Pause entre lots pour ménager le moteur, inutile quand rien n'a été appelé.
        if start + batch_size < total and not all_reused:
            await asyncio.sleep(1)

    if failures:
        raise RuntimeError(
            f"{len(failures)} clip(s) en échec sur {total}, les autres sont "
            f"conservés pour la reprise : {'; '.join(failures[:5])}"
        )
    return {"audio_clips": [path for path in clips if path is not None]}


async def resume_podcast_audio(
    output_dir: Path, episode_name: str, speaker_config: str
) -> Dict[str, Any]:
    """Reprend un épisode au stade audio : relit `transcript.json`, ne refait que les clips manquants.

    Plan et transcription ne sont pas régénérés. Même `episode_name` que la
    génération d'origine : le fichier final s'appelle `audio/<episode_name>.mp3`.
    """
    from podcast_creator.speakers import load_speaker_config

    transcript_path = Path(output_dir) / "transcript.json"
    if not transcript_path.exists():
        raise ValueError(f"Épisode non reprenable : {transcript_path} est absent")
    raw = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript = [Dialogue(**line) for line in raw]

    state: Dict[str, Any] = {
        "transcript": transcript,
        "output_dir": Path(output_dir),
        "episode_name": episode_name,
        "speaker_profile": load_speaker_config(speaker_config),
        "audio_clips": [],
    }
    state.update(await resilient_generate_all_audio_node(state))
    combined = await combine_audio_node(state, {})
    return {
        "transcript": transcript,
        "outline": None,
        "final_output_file_path": combined["final_output_file_path"],
        "audio_clips_count": len(state["audio_clips"]),
        "output_dir": Path(output_dir),
    }
