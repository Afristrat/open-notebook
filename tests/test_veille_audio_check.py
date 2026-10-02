"""Tests de la vérification du format audio (open_notebook/veille/audio_check.py).

Saqr a signalé que le moteur de voix peut rendre du WAV étiqueté audio/mpeg: un fichier WAV
nommé .mp3 ne doit JAMAIS être annoncé « pret ». Les fichiers sont générés par ffmpeg.
"""

import shutil
import subprocess

import pytest

from open_notebook.veille.audio_check import AudioInvalid, probe_mp3

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg absent")


def _tone(path, seconds, codec_args):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         *codec_args, str(path)],
        check=True,
    )


@pytest.mark.asyncio
async def test_a_real_mp3_is_accepted_with_its_duration(tmp_path):
    path = tmp_path / "ok.mp3"
    _tone(path, 70, ["-c:a", "libmp3lame", "-b:a", "64k"])
    info = await probe_mp3(path)
    assert info["codec"] == "mp3"
    assert 69 <= info["duration_s"] <= 71


@pytest.mark.asyncio
async def test_a_wav_renamed_mp3_is_refused(tmp_path):
    wav = tmp_path / "voix.wav"
    _tone(wav, 70, ["-c:a", "pcm_s16le"])
    fake = tmp_path / "episode.mp3"
    wav.rename(fake)
    with pytest.raises(AudioInvalid, match="pas un MP3"):
        await probe_mp3(fake)


@pytest.mark.asyncio
async def test_a_too_short_episode_is_refused(tmp_path):
    path = tmp_path / "court.mp3"
    _tone(path, 10, ["-c:a", "libmp3lame", "-b:a", "64k"])
    with pytest.raises(AudioInvalid, match="trop court"):
        await probe_mp3(path)


@pytest.mark.asyncio
async def test_a_missing_or_empty_file_is_refused(tmp_path):
    with pytest.raises(AudioInvalid):
        await probe_mp3(tmp_path / "absent.mp3")
    empty = tmp_path / "vide.mp3"
    empty.write_bytes(b"")
    with pytest.raises(AudioInvalid):
        await probe_mp3(empty)
