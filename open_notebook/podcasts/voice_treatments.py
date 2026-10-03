"""Réglages de rendu propres à chaque voix, tels que validés à l'écoute par Amine le 2026-10-03.

Le moteur de voix (Higgs) rend chaque voix différemment ; la sélection d'Amine associe à chacune
un réglage précis que la chaîne ne portait pas :

- Rim, version 4 : les mots anglais sont réécrits en orthographe française (« proxi », « dache
  borde ») et `language=fr` est envoyé ;
- Younes, version 2 : `language=fr` ;
- Hanae, version 1 : volume relevé de 10,6 dB pour égaler Rim (-19,7 LUFS) ;
- Mehdi, version 1 : bruit de fond nettoyé (plancher mesuré -43,9 -> -62,7 dB, voix intacte) ;
- Younes : +6,3 dB pour le même motif.

Ces réglages s'appliquent par identifiant de voix, dans tous les profils qui l'emploient. Le
lexique global de `tts_text.py` (« arXiv » devient « arksive ») reste appliqué à toutes les voix.
Le filtre audio passe APRÈS la détection de silence interne du clip (un gain remonterait le
plancher de bruit au-dessus du seuil de détection).
"""

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

# Réécriture phonétique de Rim (version 4). Mot entier, casse libre, pluriel en « s » conservé.
_RIM_RESPELLING: Tuple[Tuple[str, str], ...] = (
    ("proxy", "proxi"),
    ("LiteLLM", "laïte èl èl èm"),
    ("API", "éï pi aï"),
    ("OpenAI", "opène éï aï"),
    ("dashboard", "dache borde"),
    ("benchmark", "bèntche marke"),
)


@dataclass(frozen=True)
class VoiceTreatment:
    language: Optional[str] = None
    respelling: Tuple[Tuple[str, str], ...] = ()
    audio_filter: Optional[str] = None


NO_TREATMENT = VoiceTreatment()

VOICE_TREATMENTS: Dict[str, VoiceTreatment] = {
    "rim": VoiceTreatment(language="fr", respelling=_RIM_RESPELLING),
    "hanae": VoiceTreatment(audio_filter="volume=10.6dB"),
    "mehdi": VoiceTreatment(audio_filter="highpass=f=80:poles=2,afftdn=nr=24:nf=-44:tn=1"),
    "younes": VoiceTreatment(language="fr", audio_filter="volume=6.3dB"),
}


def treatment_for(voice_id: Optional[str]) -> VoiceTreatment:
    return VOICE_TREATMENTS.get((voice_id or "").lower(), NO_TREATMENT)


def alters_clip(treatment: VoiceTreatment) -> bool:
    """Vrai si le réglage change le clip produit (donc un clip existant sans marqueur est à refaire)."""
    return bool(treatment.language or treatment.respelling or treatment.audio_filter)


def respell(text: str, treatment: VoiceTreatment) -> str:
    """Remplace chaque mot du réglage par sa graphie sonore (mot entier, casse libre, « s » final gardé)."""
    for word, spoken in treatment.respelling:
        pattern = re.compile(rf"\b{re.escape(word)}(s?)\b", re.IGNORECASE)
        # Le remplacement passe par une chaîne (\g<1> = le « s » final éventuel), sans fonction.
        text = pattern.sub(spoken.replace("\\", "\\\\") + r"\g<1>", text)
    return text


async def apply_audio_filter(path: Path, treatment: VoiceTreatment) -> None:
    """Applique le filtre ffmpeg au clip en place (mp3 44,1 kHz stéréo 128 kbit/s, comme l'épisode).

    Lève RuntimeError si ffmpeg est absent ou échoue : le réglage validé fait partie du livrable,
    un clip non traité ne passe pas en silence.
    """
    if not treatment.audio_filter:
        return
    temp = path.with_name(f"{path.stem}.treated.mp3")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(path),
            "-af", treatment.audio_filter,
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(temp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        raise RuntimeError(f"filtre audio indisponible (ffmpeg) : {exc}") from exc
    if process.returncode != 0 or not temp.exists():
        temp.unlink(missing_ok=True)
        raise RuntimeError(
            f"filtre audio « {treatment.audio_filter} » en échec : "
            f"{stderr.decode('utf-8', 'replace')[:200]}"
        )
    os.replace(temp, path)
