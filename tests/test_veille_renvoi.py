"""Tests du renvoi à Saqr d'un épisode de veille choisi à la main (open_notebook/veille/renvoi.py).

Ni base, ni réseau: le suivi est le faux en mémoire de veille_fakes, l'épisode, la sonde du MP3 et
le rappel sont scénarisés. Ce qui est vérifié, ce sont les règles du contrat de Saqr: révision de la
demande (jamais celle d'un épisode antérieur), un seul rappel « pret », état restauré si Saqr refuse.
"""

from pathlib import Path

import pytest
from veille_fakes import FakeStore

from open_notebook.veille import renvoi, runs
from open_notebook.veille.saqr_client import SaqrRejected, SaqrUnavailable

REF = "veille-2026-10-11"
NAME = "Veille Saqr 2026-10-11"


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    monkeypatch.delenv("SAQR_VEILLE_PAGE_URL_TEMPLATE", raising=False)
    monkeypatch.setenv("DIWAN_PUBLIC_URL", "https://diwan.ai-mpower.com")
    return fake


@pytest.fixture
def sent(monkeypatch):
    payloads = []

    async def fake_post(payload):
        payloads.append(payload)

    monkeypatch.setattr(renvoi, "post_callback", fake_post)
    return payloads


@pytest.fixture
def episode(monkeypatch):
    """Un épisode terminé, créé après la demande; chaque test peut en changer les champs."""
    state = {
        "id": "episode:abc",
        "name": NAME,
        "audio_file": "episodes/abc/audio/abc.mp3",
        "apres_demande": True,
    }

    async def fake_episode(episode_id, since):
        if episode_id != state["id"]:
            raise renvoi.RenvoiRefused(404, "Épisode introuvable.")
        return dict(state)

    async def fake_probe(path):
        return {"duration_s": 701.3}

    monkeypatch.setattr(renvoi, "_episode", fake_episode)
    monkeypatch.setattr(renvoi, "probe_mp3", fake_probe)
    monkeypatch.setattr(
        renvoi,
        "resolve_contained_audio_path",
        lambda value: Path("/data/podcasts") / value,
    )
    return state


class TestNameToRef:
    def test_a_veille_name_gives_its_ref(self):
        assert renvoi.ref_of_name(NAME) == REF

    @pytest.mark.parametrize(
        "name",
        [
            None,
            "",
            "Mon podcast",
            "Veille Saqr 2026-10-11 bis",
            "veille saqr 2026-10-11",
        ],
    )
    def test_any_other_name_gives_nothing(self, name):
        assert renvoi.ref_of_name(name) is None


class TestResend:
    @pytest.mark.asyncio
    async def test_the_episode_is_linked_and_announced_with_the_revision_of_saqrs_request(
        self, store, sent, episode
    ):
        store.add(
            REF,
            revision="8d27b3ce",
            statut="echec",
            erreur="ffmpeg",
            page_url="https://saqr.ma/blog/x",
        )
        result = await renvoi.resend("episode:abc")
        assert sent == [
            {
                "ref": REF,
                "revision": "8d27b3ce",
                "statut": "pret",
                "audio_url": "https://diwan.ai-mpower.com/api/podcasts/episodes/episode:abc/audio",
                "page_url": "https://saqr.ma/blog/x",
                "duree_s": 701,
                "erreur": None,
            }
        ]
        row = store.by_ref(REF)
        assert (row["statut"], row["episode"], row["rappel_statut"]) == (
            "pret",
            "episode:abc",
            "envoye",
        )
        assert "erreur" not in row
        assert (
            result["rappel"] == "envoye"
            and result["revision"] == "8d27b3ce"
            and result["dureeS"] == 701
        )

    @pytest.mark.asyncio
    async def test_a_new_episode_replaces_the_one_already_announced(
        self, store, sent, episode
    ):
        store.add(
            REF,
            statut="pret",
            episode="episode:ancien",
            audio_url="https://d/ancien",
            duree_s=900.0,
            rappel_statut="envoye",
        )
        await renvoi.resend("episode:abc")
        assert len(sent) == 1 and sent[0]["audio_url"].endswith("episode:abc/audio")
        assert store.by_ref(REF)["episode"] == "episode:abc"

    @pytest.mark.asyncio
    async def test_an_episode_that_is_not_a_veille_is_refused_without_any_callback(
        self, store, sent, episode
    ):
        store.add(REF, statut="echec")
        episode["name"] = "Mon podcast"
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 422 and sent == []

    @pytest.mark.asyncio
    async def test_a_veille_saqr_never_asked_for_is_refused(self, store, sent, episode):
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 404 and sent == []

    @pytest.mark.asyncio
    async def test_an_unknown_episode_is_refused(self, store, sent, episode):
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:inconnu")
        assert err.value.status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("statut", sorted(runs.ACTIVE))
    async def test_a_production_in_progress_is_never_interrupted(
        self, store, sent, episode, statut
    ):
        store.add(REF, statut=statut)
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 409 and sent == []
        assert store.by_ref(REF)["statut"] == statut

    @pytest.mark.asyncio
    async def test_an_episode_older_than_saqrs_last_request_is_refused(
        self, store, sent, episode
    ):
        store.add(REF, statut="echec")
        episode["apres_demande"] = False
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 409 and sent == []
        assert "episode" not in store.by_ref(REF)

    @pytest.mark.asyncio
    async def test_an_episode_without_audio_is_refused(self, store, sent, episode):
        store.add(REF, statut="echec")
        episode["audio_file"] = None
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 409 and sent == []

    @pytest.mark.asyncio
    async def test_an_audio_outside_the_allowed_folder_is_refused(
        self, store, sent, episode, monkeypatch
    ):
        store.add(REF, statut="echec")
        monkeypatch.setattr(renvoi, "resolve_contained_audio_path", lambda value: None)
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 409 and sent == []

    @pytest.mark.asyncio
    async def test_an_invalid_mp3_is_refused(self, store, sent, episode, monkeypatch):
        store.add(REF, statut="echec")

        async def bad(path):
            raise renvoi.AudioInvalid("durée trop courte")

        monkeypatch.setattr(renvoi, "probe_mp3", bad)
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert (
            err.value.status == 409
            and "durée trop courte" in str(err.value)
            and sent == []
        )


class TestSaqrAnswers:
    @pytest.mark.asyncio
    async def test_when_saqr_refuses_the_run_goes_back_to_its_previous_state(
        self, store, episode, monkeypatch
    ):
        async def refuse(payload):
            raise SaqrRejected(
                "Le rappel refusé par Saqr (HTTP 400 : audio_hote_refuse)."
            )

        monkeypatch.setattr(renvoi, "post_callback", refuse)
        store.add(
            REF,
            statut="pret",
            episode="episode:ancien",
            audio_url="https://d/ancien",
            duree_s=900.0,
            rappel_statut="envoye",
            rappel_tentatives=2,
        )
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 502 and "audio_hote_refuse" in str(err.value)
        row = store.by_ref(REF)
        assert (row["statut"], row["episode"], row["audio_url"], row["duree_s"]) == (
            "pret",
            "episode:ancien",
            "https://d/ancien",
            900.0,
        )
        assert (row["rappel_statut"], row["rappel_tentatives"]) == ("envoye", 2)

    @pytest.mark.asyncio
    async def test_a_refusal_after_a_failure_restores_the_failure(
        self, store, episode, monkeypatch
    ):
        async def refuse(payload):
            raise SaqrRejected("refusé")

        monkeypatch.setattr(renvoi, "post_callback", refuse)
        store.add(
            REF,
            statut="echec",
            erreur="ffmpeg exited with code 245",
            rappel_statut="envoye",
        )
        with pytest.raises(renvoi.RenvoiRefused):
            await renvoi.resend("episode:abc")
        row = store.by_ref(REF)
        assert (
            row["statut"] == "echec" and row["erreur"] == "ffmpeg exited with code 245"
        )
        assert "episode" not in row and "audio_url" not in row

    @pytest.mark.asyncio
    async def test_when_saqr_is_unreachable_the_link_stays_and_the_callback_is_replayed_by_the_loop(
        self, store, episode, monkeypatch
    ):
        async def down(payload):
            raise SaqrUnavailable("injoignable")

        monkeypatch.setattr(renvoi, "post_callback", down)
        store.add(REF, statut="echec", rappel_statut="envoye")
        with pytest.raises(renvoi.RenvoiRefused) as err:
            await renvoi.resend("episode:abc")
        assert err.value.status == 503
        row = store.by_ref(REF)
        assert (row["statut"], row["episode"], row["rappel_statut"]) == (
            "pret",
            "episode:abc",
            "a_envoyer",
        )
        assert [r["ref"] for r in await runs.list_pending_callbacks()] == [REF]
