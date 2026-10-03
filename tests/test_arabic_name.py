"""Tests du prénom « Hanaa » dit en arabe (open_notebook/podcasts/arabic_name.py) et de la graphie « Médi ».

Décision d'Amine du 03/10/2026 : le prénom se dit en arabe, coupé au creux d'énergie de « معنا هناء » (aucun
« avec nous » ne doit s'entendre), assemblé à la réplique française dans la voix de celui qui parle ; « Mehdi »
s'écrit « Médi » pour le moteur.
"""

import array
import math
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from podcast_creator.core import Dialogue

from open_notebook.podcasts import arabic_name as an
from open_notebook.podcasts import resilient_audio as ra
from open_notebook.podcasts.tts_text import apply_lexicon


def tone(seconds: float, amplitude: int = 12000, freq: int = 220) -> array.array:
    count = int(an.SAMPLE_RATE * seconds)
    return array.array(
        "h", [int(amplitude * math.sin(2 * math.pi * freq * i / an.SAMPLE_RATE)) for i in range(count)]
    )


def flat(seconds: float, level: int = 0) -> array.array:
    return array.array("h", [level] * int(an.SAMPLE_RATE * seconds))


class TestSplit:
    def test_a_name_between_two_french_parts(self):
        assert an.split_around_name("Merci Hanaa pour ce terrain.") == [
            (False, "Merci "), (True, "Hanaa"), (False, " pour ce terrain."),
        ]

    def test_a_name_at_the_start_has_no_empty_french_part(self):
        assert an.split_around_name("Hanaa, qu'en penses-tu ?") == [(True, "Hanaa"), (False, ", qu'en penses-tu ?")]

    def test_punctuation_alone_is_dropped_and_case_is_free(self):
        assert an.split_around_name("Merci hanaa.") == [(False, "Merci "), (True, "hanaa")]

    def test_two_occurrences(self):
        parts = an.split_around_name("Hanaa, voici ce que dit Hanaa sur le terrain.")
        assert [is_name for is_name, _ in parts] == [True, False, True, False]

    def test_no_name_no_arabic(self):
        assert an.has_arabic_name("Rien à signaler.") is False
        assert an.has_arabic_name("Écoutons Hanaa.") is True
        assert an.has_arabic_name("Hanaaa") is False

    def test_a_fragment_too_short_for_the_engine_is_padded(self):
        assert an.pad_fragment("Merci ") == "Merci ..."
        assert an.pad_fragment("Oui, ") == "Oui ..."
        assert an.pad_fragment(", qu'en penses-tu ?") == ", qu'en penses-tu ?"


class TestCut:
    def test_the_name_is_what_follows_the_dip_between_the_two_words(self):
        carrier = flat(0.30) + tone(0.35) + flat(0.04, 150) + tone(0.40) + flat(0.10)
        clip = an.cut_after_carrier(carrier)
        seconds = len(clip) / an.SAMPLE_RATE
        assert 0.38 <= seconds <= 0.55
        assert max(abs(x) for x in clip) > 10000

    def test_the_cut_fades_in_to_avoid_a_click(self):
        carrier = flat(0.30) + tone(0.35) + flat(0.04, 150) + tone(0.40) + flat(0.10)
        assert an.cut_after_carrier(carrier)[0] == 0

    def test_silence_is_refused(self):
        with pytest.raises(ValueError):
            an.cut_after_carrier(flat(1.0))


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg absent")
class TestAssemble:
    @pytest.mark.asyncio
    async def test_the_parts_become_one_mp3_of_the_summed_length(self, tmp_path):
        parts = []
        for number in range(2):
            path = tmp_path / f"{number}.wav"
            an.write_wav(path, tone(0.5))
            parts.append(path)
        out = tmp_path / "0000.mp3"
        await an.assemble(parts, out)
        seconds = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(out)],
                capture_output=True, text=True,
            ).stdout
        )
        assert 1.0 <= seconds <= 1.3
        assert not list(tmp_path.glob("*.assemble*"))


class TestLexicon:
    def test_mehdi_is_written_for_the_engine_with_its_accent(self):
        assert apply_lexicon("Mehdi et mehdi, MEHDI.") == "Médi et Médi, Médi."

    def test_only_the_whole_word(self):
        assert apply_lexicon("Mehdia") == "Mehdia"


def make_info(tmp_path: Path, speaker: str, voice: str, text: str) -> Dict[str, Any]:
    return {
        "dialogue": Dialogue(speaker=speaker, dialogue=text),
        "index": 0,
        "output_dir": tmp_path,
        "voices": {speaker: voice},
        "tts_provider": "openai-compatible",
        "tts_model": "higgs",
        "tts_config": {},
    }


@pytest.fixture
def chain(monkeypatch):
    events: List[tuple] = []

    async def speak(info, text, output_file, language):
        events.append(("speak", text, language))
        output_file.write_bytes(b"x" * 2000)

    async def name_clip(voice, output_wav):
        events.append(("nom", voice))
        output_wav.write_bytes(b"x" * 2000)

    async def assemble(parts, output_mp3):
        events.append(("assemble", [part.suffix for part in parts]))
        output_mp3.write_bytes(b"x" * 2000)

    async def library_clip(info):
        events.append(("librairie", info["dialogue"].dialogue))
        path = ra._clip_path(info["output_dir"], info["index"])
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(b"x" * 2000)
        return path

    async def language_clip(info, language):
        events.append(("langue", language, info["dialogue"].dialogue))
        return await library_clip(info)

    async def silence(_path):
        return 0.0

    async def audio_filter(_path, treatment):
        events.append(("filtre", treatment.audio_filter))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(ra, "_speak", speak)
    monkeypatch.setattr(ra.arabic_name, "fetch_name_clip", name_clip)
    monkeypatch.setattr(ra.arabic_name, "assemble", assemble)
    monkeypatch.setattr(ra, "generate_single_audio_clip", library_clip)
    monkeypatch.setattr(ra, "_generate_clip_with_language", language_clip)
    monkeypatch.setattr(ra, "_longest_silence", silence)
    monkeypatch.setattr(ra, "apply_audio_filter", audio_filter)
    monkeypatch.setattr(ra.asyncio, "sleep", no_sleep)
    monkeypatch.setenv("DIWAN_TTS_MAX_ATTEMPTS", "2")
    return SimpleNamespace(events=events)


class TestClipWithArabicName:
    @pytest.mark.asyncio
    async def test_the_name_is_spoken_in_arabic_in_the_voice_of_the_speaker(self, tmp_path, chain):
        info = make_info(tmp_path, "Mehdi", "mehdi", "Hanaa, qu'en penses-tu de ce chiffre ?")

        await ra.synthesize_clip_resilient(info)

        assert chain.events[0] == ("nom", "mehdi")
        assert chain.events[1][0] == "speak" and "qu'en penses-tu" in chain.events[1][1]
        assert chain.events[1][2] is None
        assert ("assemble", [".wav", ".mp3"]) in chain.events
        assert [e[0] for e in chain.events].count("librairie") == 0

    @pytest.mark.asyncio
    async def test_a_fragment_before_the_name_is_padded_and_keeps_the_voice_language(self, tmp_path, chain):
        info = make_info(tmp_path, "Rim", "rim", "Merci Hanaa.")

        await ra.synthesize_clip_resilient(info)

        assert ("speak", "Merci ...", "fr") in chain.events
        assert ("nom", "rim") in chain.events

    @pytest.mark.asyncio
    async def test_two_occurrences_give_two_arabic_names(self, tmp_path, chain):
        info = make_info(tmp_path, "Younes", "younes", "Hanaa, voici ce que dit Hanaa sur le terrain au Maroc.")

        await ra.synthesize_clip_resilient(info)

        assert [e for e in chain.events if e[0] == "nom"] == [("nom", "younes"), ("nom", "younes")]
        assert ("assemble", [".wav", ".mp3", ".wav", ".mp3"]) in chain.events

    @pytest.mark.asyncio
    async def test_a_broken_arabic_endpoint_leaves_the_reply_in_french_instead_of_losing_the_episode(
        self, tmp_path, chain, monkeypatch
    ):
        async def broken(voice, output_wav):
            raise RuntimeError("HTTP 500")

        monkeypatch.setattr(ra.arabic_name, "fetch_name_clip", broken)
        info = make_info(tmp_path, "Hanaa", "hanae", "Merci beaucoup, c'est Hanaa qui parle ici.")

        path = await ra.synthesize_clip_resilient(info)

        assert path.exists()
        assert ("librairie", "Merci beaucoup, c'est Hanaa qui parle ici.") in chain.events
        assert not (tmp_path / "arabic_name" / "0000").exists()

    @pytest.mark.asyncio
    async def test_a_reply_without_the_name_is_untouched(self, tmp_path, chain):
        info = make_info(tmp_path, "Mehdi", "mehdi", "La source indique un chiffre précis.")

        await ra.synthesize_clip_resilient(info)

        assert [e[0] for e in chain.events] == ["librairie", "filtre"]

    @pytest.mark.asyncio
    async def test_a_clip_made_before_the_arabic_name_is_regenerated(self, tmp_path, chain):
        old = tmp_path / "clips" / "0000.mp3"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"y" * 2000)
        info = make_info(tmp_path, "Mehdi", "mehdi", "Hanaa, qu'en penses-tu de ce chiffre ?")

        await ra.synthesize_clip_resilient(info)

        assert ("nom", "mehdi") in chain.events
