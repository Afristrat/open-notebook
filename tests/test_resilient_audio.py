"""Tests du nœud audio résilient (reprise au clip, texte normalisé).

Le moteur de voix de production échoue de façon déterministe sur les phrases
de moins de 7 caractères : le nœud réécrit le texte, change d'écriture après un
échec, reprend les clips déjà produits et ne perd jamais les clips réussis.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional, Tuple

import pytest
from podcast_creator.core import Dialogue

from open_notebook.podcasts import resilient_audio as ra


def make_profile():
    speaker = SimpleNamespace(tts_provider=None, tts_model=None, tts_config=None)
    return SimpleNamespace(
        tts_provider="provider",
        tts_model="model",
        tts_config={},
        get_voice_mapping=lambda: {"Rim": "voice"},
        get_speaker_by_name=lambda name: speaker,
    )


def make_state(tmp_path: Path, lines: List[str]):
    return {
        "transcript": [Dialogue(speaker="Rim", dialogue=line) for line in lines],
        "output_dir": tmp_path,
        "speaker_profile": make_profile(),
    }


@pytest.fixture
def synth(monkeypatch):
    """Remplace la synthèse de la librairie ; renvoie la liste des appels (index, texte)."""
    calls: List[Tuple[int, str]] = []
    behaviour: dict = {"fail_if": lambda index, text: False, "raises": RuntimeError}

    async def fake_clip(info):
        index, text = info["index"], info["dialogue"].dialogue
        calls.append((index, text))
        if behaviour["fail_if"](index, text):
            raise behaviour["raises"]("HTTP 500: Internal Server Error")
        path = Path(info["output_dir"]) / "clips" / f"{index:04d}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 2000)
        return path

    async def no_sleep(_seconds):
        return None

    silences: List[float] = []

    async def fake_silence(_path):
        return silences.pop(0) if silences else 0.0

    monkeypatch.setattr(ra, "generate_single_audio_clip", fake_clip)
    monkeypatch.setattr(ra, "_longest_silence", fake_silence)
    monkeypatch.setattr(ra.asyncio, "sleep", no_sleep)
    monkeypatch.setenv("DIWAN_TTS_WAIT_BASE", "0")
    monkeypatch.setenv("DIWAN_TTS_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("TTS_BATCH_SIZE", "2")

    def configure(
        fail_if: Optional[Callable[[int, str], bool]] = None, raises=RuntimeError
    ):
        if fail_if is not None:
            behaviour["fail_if"] = fail_if
        behaviour["raises"] = raises

    return SimpleNamespace(calls=calls, configure=configure, silences=silences)


class TestResilientAudioNode:
    @pytest.mark.asyncio
    async def test_all_clips_are_produced(self, tmp_path, synth):
        state = make_state(tmp_path, ["Première réplique.", "Deuxième réplique.", "Fin."])

        result = await ra.resilient_generate_all_audio_node(state)

        assert len(result["audio_clips"]) == 3
        assert all(path.exists() for path in result["audio_clips"])

    @pytest.mark.asyncio
    async def test_short_opening_is_rewritten_before_any_failure(self, tmp_path, synth):
        synth.configure(fail_if=lambda i, text: text.startswith("Oui."))
        state = make_state(tmp_path, ["Oui. L'humain reste décideur."])

        await ra.resilient_generate_all_audio_node(state)

        assert synth.calls == [(0, "Oui, L'humain reste décideur.")]

    @pytest.mark.asyncio
    async def test_another_writing_is_tried_after_a_failure(self, tmp_path, synth):
        synth.configure(fail_if=lambda i, text: text.startswith("Oui,"))
        state = make_state(tmp_path, ["Oui. L'humain reste décideur."])

        await ra.resilient_generate_all_audio_node(state)

        assert [text for _, text in synth.calls] == [
            "Oui, L'humain reste décideur.",
            "Oui. L'humain reste décideur.",
        ]

    @pytest.mark.asyncio
    async def test_existing_clip_is_reused_not_regenerated(self, tmp_path, synth):
        existing = tmp_path / "clips" / "0000.mp3"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"x" * 2000)
        state = make_state(tmp_path, ["Première réplique.", "Deuxième réplique."])

        await ra.resilient_generate_all_audio_node(state)

        assert [index for index, _ in synth.calls] == [1]

    @pytest.mark.asyncio
    async def test_truncated_clip_is_regenerated(self, tmp_path, synth):
        broken = tmp_path / "clips" / "0000.mp3"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"x" * 10)
        state = make_state(tmp_path, ["Première réplique."])

        await ra.resilient_generate_all_audio_node(state)

        assert [index for index, _ in synth.calls] == [0]
        assert broken.stat().st_size >= ra.MIN_CLIP_BYTES

    @pytest.mark.asyncio
    async def test_failed_clip_does_not_discard_the_others(
        self, tmp_path, synth, monkeypatch
    ):
        monkeypatch.setenv("DIWAN_TTS_MAX_ATTEMPTS", "2")
        synth.configure(fail_if=lambda i, text: "IMPOSSIBLE" in text)
        state = make_state(
            tmp_path,
            ["Première réplique.", "Réplique IMPOSSIBLE à dire.", "Troisième réplique."],
        )

        with pytest.raises(RuntimeError, match="0001"):
            await ra.resilient_generate_all_audio_node(state)

        assert (tmp_path / "clips" / "0000.mp3").exists()
        assert (tmp_path / "clips" / "0002.mp3").exists()
        assert not (tmp_path / "clips" / "0001.mp3").exists()

    @pytest.mark.asyncio
    async def test_non_retryable_error_is_not_retried(self, tmp_path, synth):
        synth.configure(fail_if=lambda i, text: True, raises=KeyError)
        state = make_state(tmp_path, ["Réplique quelconque."])

        with pytest.raises(RuntimeError, match="0000"):
            await ra.resilient_generate_all_audio_node(state)

        assert len(synth.calls) == 1

    @pytest.mark.asyncio
    async def test_missing_speaker_profile_is_a_value_error(self, tmp_path, synth):
        state = make_state(tmp_path, ["Réplique."])
        state["speaker_profile"] = None

        with pytest.raises(ValueError):
            await ra.resilient_generate_all_audio_node(state)


class TestSilenceDetection:
    @pytest.mark.asyncio
    async def test_generated_clip_with_a_long_silence_is_regenerated(
        self, tmp_path, synth
    ):
        synth.silences.extend([80.0, 0.0])
        state = make_state(tmp_path, ["Une réplique assez longue pour être dite."])

        await ra.resilient_generate_all_audio_node(state)

        assert [index for index, _ in synth.calls] == [0, 0]

    @pytest.mark.asyncio
    async def test_existing_clip_with_a_long_silence_is_replaced_on_resume(
        self, tmp_path, synth
    ):
        existing = tmp_path / "clips" / "0000.mp3"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"x" * 2000)
        synth.silences.extend([76.0, 0.0])
        state = make_state(tmp_path, ["Une réplique assez longue pour être dite."])

        await ra.resilient_generate_all_audio_node(state)

        assert [index for index, _ in synth.calls] == [0]

    @pytest.mark.asyncio
    async def test_clip_that_stays_silent_fails_after_all_attempts(
        self, tmp_path, synth, monkeypatch
    ):
        monkeypatch.setenv("DIWAN_TTS_MAX_ATTEMPTS", "2")
        synth.silences.extend([50.0, 50.0])
        state = make_state(tmp_path, ["Une réplique assez longue pour être dite."])

        with pytest.raises(RuntimeError, match="silence interne"):
            await ra.resilient_generate_all_audio_node(state)

        assert not (tmp_path / "clips" / "0000.mp3").exists()


class TestLongestSilence:
    @pytest.mark.asyncio
    async def test_reads_the_longest_silence_from_ffmpeg_output(
        self, tmp_path, monkeypatch
    ):
        class FakeProcess:
            async def communicate(self):
                return b"", b"silence_duration: 12.5\nsilence_duration: 3.0\n"

        async def fake_exec(*_args, **_kwargs):
            return FakeProcess()

        monkeypatch.setattr(ra.asyncio, "create_subprocess_exec", fake_exec)

        assert await ra._longest_silence(tmp_path / "clip.mp3") == 12.5

    @pytest.mark.asyncio
    async def test_missing_ffmpeg_keeps_the_clip(self, tmp_path, monkeypatch):
        async def missing(*_args, **_kwargs):
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr(ra.asyncio, "create_subprocess_exec", missing)

        assert await ra._longest_silence(tmp_path / "clip.mp3") == 0.0


class TestGraphInstall:
    def test_graph_compiles_with_the_guard_and_the_resilient_audio_nodes(self):
        import podcast_creator.graph as pcg

        from open_notebook.podcasts import vocalization

        assert vocalization.ensure_vocalization_installed() is True
        nodes = set(pcg.graph.get_graph().nodes)
        assert {"vocalize_transcript", "content_guard", "generate_all_audio"} <= nodes


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_requires_the_transcript(self, tmp_path):
        with pytest.raises(ValueError, match="transcript.json"):
            await ra.resume_podcast_audio(tmp_path, "episode", "profil")
