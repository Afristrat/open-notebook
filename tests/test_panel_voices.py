"""Tests du panel Éclaireurs : réglages de rendu par voix et partage de parole à quatre voix.

Sélection validée à l'écoute par Amine le 2026-10-03 : Rim version 4 (réécriture phonétique +
language=fr), Younes version 2 (language=fr, +6,3 dB), Hanae version 1 (+10,6 dB), Mehdi
version 1 (bruit nettoyé). Partage : chaque invité +5 points, prélevés sur la modératrice.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest
from podcast_creator.core import Dialogue

from open_notebook.podcasts import content_guard as cg
from open_notebook.podcasts import resilient_audio as ra
from open_notebook.podcasts import voice_treatments as vt


class TestVoiceTreatments:
    def test_rim_rewrites_english_words_phonetically(self):
        text = "Le proxy et les APIs du Dashboard passent le benchmark."
        assert vt.respell(text, vt.treatment_for("rim")) == (
            "Le proxi et les éï pi aïs du dache borde passent le bèntche marke."
        )

    def test_rewriting_matches_whole_words_only(self):
        assert vt.respell("Une capitale apicole.", vt.treatment_for("rim")) == "Une capitale apicole."

    def test_rim_and_younes_send_french(self):
        assert vt.treatment_for("rim").language == "fr"
        assert vt.treatment_for("Younes").language == "fr"

    def test_hanae_and_mehdi_send_no_language(self):
        assert vt.treatment_for("hanae").language is None
        assert vt.treatment_for("mehdi").language is None

    def test_levels_and_denoise_are_the_validated_ones(self):
        assert vt.treatment_for("hanae").audio_filter == "volume=10.6dB"
        assert vt.treatment_for("younes").audio_filter == "volume=6.3dB"
        assert vt.treatment_for("mehdi").audio_filter == (
            "highpass=f=80:poles=2,afftdn=nr=24:nf=-44:tn=1"
        )

    def test_other_voices_are_untouched(self):
        for voice in ("khalid", "tariq", "voice", None):
            assert vt.alters_clip(vt.treatment_for(voice)) is False


class TestAudioFilter:
    @pytest.mark.asyncio
    async def test_the_clip_is_replaced_by_the_filtered_one(self, tmp_path, monkeypatch):
        clip = tmp_path / "0000.mp3"
        clip.write_bytes(b"avant")
        commands: List[tuple] = []

        class Process:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **_kwargs):
            commands.append(args)
            Path(args[-1]).write_bytes(b"apres")
            return Process()

        monkeypatch.setattr(vt.asyncio, "create_subprocess_exec", fake_exec)

        await vt.apply_audio_filter(clip, vt.treatment_for("hanae"))

        assert clip.read_bytes() == b"apres"
        assert "volume=10.6dB" in commands[0]
        assert not (tmp_path / "0000.treated.mp3").exists()

    @pytest.mark.asyncio
    async def test_a_failing_filter_raises_and_keeps_the_clip(self, tmp_path, monkeypatch):
        clip = tmp_path / "0000.mp3"
        clip.write_bytes(b"avant")

        class Process:
            returncode = 1

            async def communicate(self):
                return b"", b"Invalid argument"

        async def fake_exec(*_args, **_kwargs):
            return Process()

        monkeypatch.setattr(vt.asyncio, "create_subprocess_exec", fake_exec)

        with pytest.raises(RuntimeError, match="en échec"):
            await vt.apply_audio_filter(clip, vt.treatment_for("mehdi"))
        assert clip.read_bytes() == b"avant"

    @pytest.mark.asyncio
    async def test_missing_ffmpeg_is_not_silent(self, tmp_path, monkeypatch):
        clip = tmp_path / "0000.mp3"
        clip.write_bytes(b"avant")

        async def missing(*_args, **_kwargs):
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr(vt.asyncio, "create_subprocess_exec", missing)

        with pytest.raises(RuntimeError, match="indisponible"):
            await vt.apply_audio_filter(clip, vt.treatment_for("younes"))

    @pytest.mark.asyncio
    async def test_no_filter_means_no_ffmpeg(self, tmp_path, monkeypatch):
        async def boom(*_args, **_kwargs):
            raise AssertionError("ffmpeg ne doit pas être appelé")

        monkeypatch.setattr(vt.asyncio, "create_subprocess_exec", boom)

        await vt.apply_audio_filter(tmp_path / "0000.mp3", vt.treatment_for("rim"))


def make_state(tmp_path: Path, speaker: str, voice: str, line: str):
    member = SimpleNamespace(tts_provider=None, tts_model=None, tts_config=None)
    profile = SimpleNamespace(
        tts_provider="provider",
        tts_model="model",
        tts_config={},
        get_voice_mapping=lambda: {speaker: voice},
        get_speaker_by_name=lambda name: member,
    )
    return {
        "transcript": [Dialogue(speaker=speaker, dialogue=line)],
        "output_dir": tmp_path,
        "speaker_profile": profile,
    }


@pytest.fixture
def chain(monkeypatch):
    """Remplace synthèse, détection de silence et filtre ; enregistre l'ordre des étapes."""
    events: List[tuple] = []

    def write(info):
        path = Path(info["output_dir"]) / "clips" / f"{info['index']:04d}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 2000)
        return path

    async def library_clip(info):
        events.append(("librairie", info["dialogue"].dialogue))
        return write(info)

    async def language_clip(info, language):
        events.append(("language", language, info["dialogue"].dialogue))
        return write(info)

    async def silence(_path):
        events.append(("silence",))
        return 0.0

    async def audio_filter(_path, treatment):
        events.append(("filtre", treatment.audio_filter))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(ra, "generate_single_audio_clip", library_clip)
    monkeypatch.setattr(ra, "_generate_clip_with_language", language_clip)
    monkeypatch.setattr(ra, "_longest_silence", silence)
    monkeypatch.setattr(ra, "apply_audio_filter", audio_filter)
    monkeypatch.setattr(ra.asyncio, "sleep", no_sleep)
    monkeypatch.setenv("DIWAN_TTS_MAX_ATTEMPTS", "2")
    return events


class TestResilientChainWithTreatments:
    @pytest.mark.asyncio
    async def test_rim_sends_french_with_the_rewritten_text(self, tmp_path, chain):
        state = make_state(tmp_path, "Rim", "rim", "Le proxy du jour est en ligne.")

        await ra.resilient_generate_all_audio_node(state)

        assert chain == [
            ("language", "fr", "Le proxi du jour est en ligne."),
            ("silence",),
            ("filtre", None),
        ]

    @pytest.mark.asyncio
    async def test_younes_sends_french_then_filters_after_the_silence_check(self, tmp_path, chain):
        state = make_state(tmp_path, "Younes", "younes", "Cette réserve mérite d'être posée.")

        await ra.resilient_generate_all_audio_node(state)

        assert [event[0] for event in chain] == ["language", "silence", "filtre"]
        assert chain[-1] == ("filtre", "volume=6.3dB")

    @pytest.mark.asyncio
    async def test_hanae_uses_the_library_call_without_language(self, tmp_path, chain):
        state = make_state(tmp_path, "Hanae", "hanae", "Sur le terrain, la demande existe.")

        await ra.resilient_generate_all_audio_node(state)

        assert [event[0] for event in chain] == ["librairie", "silence", "filtre"]
        assert chain[-1] == ("filtre", "volume=10.6dB")

    @pytest.mark.asyncio
    async def test_an_untreated_voice_keeps_the_previous_behaviour(self, tmp_path, chain):
        state = make_state(tmp_path, "Khalid", "khalid", "La source indique un chiffre.")

        await ra.resilient_generate_all_audio_node(state)

        assert [event[0] for event in chain] == ["librairie", "silence", "filtre"]
        assert chain[-1] == ("filtre", None)

    @pytest.mark.asyncio
    async def test_a_clip_made_before_the_treatments_is_regenerated(self, tmp_path, chain):
        old = tmp_path / "clips" / "0000.mp3"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x" * 2000)
        state = make_state(tmp_path, "Hanae", "hanae", "Sur le terrain, la demande existe.")

        await ra.resilient_generate_all_audio_node(state)

        assert ("librairie", "Sur le terrain, la demande existe.") in chain
        assert (tmp_path / "lexicon_checked" / "0000").exists()

    @pytest.mark.asyncio
    async def test_a_clip_already_treated_is_reused(self, tmp_path, chain):
        old = tmp_path / "clips" / "0000.mp3"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x" * 2000)
        marker = tmp_path / "lexicon_checked" / "0000"
        marker.parent.mkdir(parents=True)
        marker.touch()
        state = make_state(tmp_path, "Hanae", "hanae", "Sur le terrain, la demande existe.")

        await ra.resilient_generate_all_audio_node(state)

        assert [event[0] for event in chain] == ["silence"]


class TestPanelShare:
    NAMES = ["Rim", "Mehdi", "Hanae", "Younes"]

    def run(self, rim, mehdi, hanae, younes):
        speakers = ["Rim"] * rim + ["Mehdi"] * mehdi + ["Hanae"] * hanae + ["Younes"] * younes
        return cg.check_transcript(
            ["texte"] * len(speakers),
            "texte",
            speaker_names=self.NAMES,
            speakers=speakers,
            min_share=cg.MIN_SPEAKER_SHARE,
            panel_host="Rim",
            guest_bonus=cg.PANEL_GUEST_BONUS,
            host_min_share=cg.HOST_PANEL_MIN_SHARE,
        )

    def test_the_decided_split_is_accepted(self):
        assert self.run(19, 27, 27, 27).violations == []

    def test_a_guest_under_twenty_seven_percent_is_refused(self):
        report = self.run(20, 27, 27, 26)
        assert [v.kind for v in report.violations] == ["part_intervenant"]
        assert report.violations[0].detail.startswith("Younes ne dit que 26 réplique(s) sur 100 (26%)")
        assert "minimum 27%" in report.violations[0].detail

    def test_the_host_floor_is_lowered_but_not_removed(self):
        assert self.run(10, 30, 30, 30).violations == []
        report = self.run(9, 30, 30, 31)
        assert [v.kind for v in report.violations] == ["part_intervenant"]
        assert report.violations[0].detail.startswith("Rim ne dit que 9 réplique(s)")

    def test_a_three_voice_profile_keeps_twenty_two_percent_for_everyone(self):
        speakers = ["Rim"] * 23 + ["Khalid"] * 23 + ["Tariq"] * 54
        report = cg.check_transcript(
            ["texte"] * 100,
            "texte",
            speaker_names=["Rim", "Khalid", "Tariq"],
            speakers=speakers,
            min_share=cg.MIN_SPEAKER_SHARE,
        )
        assert report.violations == []
