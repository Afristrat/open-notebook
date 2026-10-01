"""Tests de l'endpoint de reprise d'un épisode de podcast.

Un épisode dont la transcription existe est repris au stade audio (clips
manquants ou défectueux refaits, les autres réutilisés) : il n'est jamais
supprimé. Un épisode terminé ne peut être repris que par cette voie.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.routers.podcasts import retry_podcast_episode


def make_episode(tmp_path, status, with_transcript):
    if with_transcript:
        (tmp_path / "transcript.json").write_text("[]", encoding="utf-8")
    return SimpleNamespace(
        output_dir=str(tmp_path),
        audio_file=None,
        name="Veille",
        content="contenu",
        episode_profile={"name": "Veille"},
        speaker_profile={"name": "veille"},
        get_job_detail=AsyncMock(return_value={"status": status, "error_message": None}),
        delete=AsyncMock(),
    )


@pytest.fixture
def service():
    with patch("api.routers.podcasts.PodcastService") as mocked:
        mocked.submit_generation_job = AsyncMock(return_value="command:job1")
        yield mocked


class TestRetryPodcastEpisode:
    @pytest.mark.asyncio
    async def test_failed_episode_with_transcript_is_resumed_not_deleted(
        self, tmp_path, service
    ):
        episode = make_episode(tmp_path, "failed", with_transcript=True)
        service.get_episode = AsyncMock(return_value=episode)

        result = await retry_podcast_episode("episode:abc")

        assert result["resumed"] is True
        episode.delete.assert_not_called()
        assert (
            service.submit_generation_job.await_args.kwargs["resume_episode_id"]
            == "episode:abc"
        )

    @pytest.mark.asyncio
    async def test_completed_episode_with_transcript_can_be_resumed(
        self, tmp_path, service
    ):
        episode = make_episode(tmp_path, "completed", with_transcript=True)
        service.get_episode = AsyncMock(return_value=episode)

        result = await retry_podcast_episode("episode:abc")

        assert result["resumed"] is True
        episode.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_episode_without_transcript_is_refused_and_kept(
        self, tmp_path, service
    ):
        episode = make_episode(tmp_path, "completed", with_transcript=False)
        service.get_episode = AsyncMock(return_value=episode)

        with pytest.raises(HTTPException) as error:
            await retry_podcast_episode("episode:abc")

        assert error.value.status_code == 400
        episode.delete.assert_not_called()
        service.submit_generation_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_running_episode_is_refused(self, tmp_path, service):
        episode = make_episode(tmp_path, "running", with_transcript=True)
        service.get_episode = AsyncMock(return_value=episode)

        with pytest.raises(HTTPException) as error:
            await retry_podcast_episode("episode:abc")

        assert error.value.status_code == 400

    @pytest.mark.asyncio
    async def test_failed_episode_without_transcript_is_regenerated(
        self, tmp_path, service
    ):
        episode = make_episode(tmp_path, "failed", with_transcript=False)
        service.get_episode = AsyncMock(return_value=episode)

        result = await retry_podcast_episode("episode:abc")

        assert "resumed" not in result
        episode.delete.assert_awaited_once()
        assert "resume_episode_id" not in service.submit_generation_job.await_args.kwargs
