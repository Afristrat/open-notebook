"""Liste paginée des épisodes (5 par page) : légère en mémoire, compteurs sur l'ensemble.

Le 14/10/2026 la liste complète chargeait 334 transcriptions d'un coup : l'API était tuée (code 137) et
l'interface affichait « Échec du chargement des épisodes ».
"""

from unittest.mock import AsyncMock, patch

import pytest

from api.routers.podcasts import _status_group, list_podcast_episodes_page
from open_notebook.podcasts.models import PodcastEpisode


def _episode(n: int) -> PodcastEpisode:
    return PodcastEpisode(
        id=f"episode:e{n}",
        name=f"Épisode {n}",
        episode_profile={"name": "p"},
        speaker_profile={"name": "s"},
        briefing="b",
        content="c",
        command=f"command:c{n}",
    )


def _patched(stubs, details, loaded):
    return (
        patch(
            "api.routers.podcasts.PodcastService.list_episode_stubs",
            new=AsyncMock(return_value=stubs),
        ),
        patch(
            "api.routers.podcasts.PodcastService.get_episodes",
            new=AsyncMock(return_value=loaded),
        ),
        patch(
            "api.routers.podcasts.PodcastEpisode.get_job_details_for_commands",
            new=AsyncMock(return_value=details),
        ),
        patch(
            "api.routers.podcasts._resolve_snapshot_models",
            new=AsyncMock(return_value={}),
        ),
    )


@pytest.mark.asyncio
async def test_a_page_loads_only_its_own_episodes_and_counts_all_of_them():
    stubs = [
        {"id": f"episode:e{n}", "command": f"command:c{n}", "audio_file": None}
        for n in range(1, 13)
    ]
    statuses = ["completed"] * 7 + ["failed"] * 2 + ["running"] + ["new"] * 2
    details = {
        f"command:c{n}": {"status": statuses[n - 1], "error_message": None}
        for n in range(1, 13)
    }
    loaded = [_episode(n) for n in (6, 7, 8, 9, 10)]
    p1, p2, p3, p4 = _patched(stubs, details, loaded)
    with p1, p2 as get_episodes, p3, p4:
        page = await list_podcast_episodes_page(limit=5, offset=5)

    get_episodes.assert_awaited_once_with([f"episode:e{n}" for n in (6, 7, 8, 9, 10)])
    assert [item.id for item in page.items] == [
        f"episode:e{n}" for n in (6, 7, 8, 9, 10)
    ]
    assert page.counts.model_dump() == {
        "total": 12,
        "completed": 7,
        "failed": 2,
        "running": 1,
        "pending": 2,
    }


@pytest.mark.asyncio
async def test_an_empty_database_gives_an_empty_page():
    p1, p2, p3, p4 = _patched([], {}, [])
    with p1, p2, p3, p4:
        page = await list_podcast_episodes_page(limit=5, offset=0)
    assert page.items == []
    assert page.counts.total == 0


def test_status_groups_match_the_frontend_grouping():
    assert _status_group("processing") == "running"
    assert _status_group("error") == "failed"
    assert _status_group("unknown") == "pending"
    assert _status_group("completed") == "completed"
