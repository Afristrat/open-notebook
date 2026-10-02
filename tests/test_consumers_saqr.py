"""Tests du contrat Saqr (api/routers/consumers_saqr.py).

Même esprit que test_qalem_facade_security.py: ni base ni file d'attente. Sont vérifiés l'accès
fermé par défaut, l'idempotence sur (ref, revision), le conflit de révision et la relance après
échec, qui sont les points du contrat convenu avec Saqr.
"""

import hashlib

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from veille_fakes import FakeStore

from open_notebook.consumers import auth as consumer_auth
from open_notebook.consumers.errors import ConsumerAPIError, consumer_error_response

SAQR_TOKEN = "jeton-de-test-saqr-jamais-en-production"
QALEM_TOKEN = "jeton-de-test-qalem-jamais-en-production"
URL = "/api/v1/consumers/saqr/veille-podcast"
BODY = {
    "ref": "veille-2026-10-02", "revision": "hash-1", "titre": "Harnais et sandbox",
    "date_publication": "2026-10-02", "url": "https://saqr.ma/blog/veille/veille-2026-10-02",
}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _auth(token: str = SAQR_TOKEN):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def submitted():
    return []


@pytest.fixture
def store(monkeypatch):
    return FakeStore().install(monkeypatch)


@pytest.fixture
def client(monkeypatch, submitted, store):
    monkeypatch.setenv(
        "DIWAN_CONSUMER_TOKENS",
        f"saqr:saqr:{_digest(SAQR_TOKEN)},qalem:org-alpha:{_digest(QALEM_TOKEN)}",
    )
    consumer_auth._INGEST_LIMITER._calls.clear()
    consumer_auth._READ_LIMITER._calls.clear()

    from api.routers import consumers_saqr

    def fake_submit(ref):
        submitted.append(ref)
        return f"command:{len(submitted)}"

    monkeypatch.setattr(consumers_saqr.pipeline, "submit_orchestrator", fake_submit)

    app = FastAPI()
    app.include_router(consumers_saqr.router, prefix="/api")

    @app.exception_handler(ConsumerAPIError)
    async def _handler(request: Request, exc: ConsumerAPIError):
        return consumer_error_response(request, exc)

    return TestClient(app, raise_server_exceptions=False)


class TestAccess:
    def test_without_a_token_nothing_answers(self, client):
        response = client.post(URL, json=BODY)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_another_consumers_token_does_not_open_this_door(self, client):
        response = client.post(URL, json=BODY, headers=_auth(QALEM_TOKEN))
        assert response.status_code == 403

    def test_the_token_never_appears_in_a_response(self, client):
        response = client.post(URL, json=BODY, headers=_auth(SAQR_TOKEN + "x"))
        assert SAQR_TOKEN not in response.text

    def test_the_status_route_is_closed_too(self, client):
        assert client.get(f"{URL}/veille-2026-10-02").status_code == 401


class TestRequest:
    def test_a_new_request_is_accepted_at_once_and_queued(self, client, submitted):
        response = client.post(URL, json=BODY, headers=_auth())
        assert response.status_code == 202
        body = response.json()
        assert body["ref"] == "veille-2026-10-02" and body["statut"] == "accepte"
        assert body["idempotent"] is False and body["contractVersion"] == "1.0"
        assert submitted == ["veille-2026-10-02"]

    def test_the_same_ref_and_revision_is_idempotent(self, client, submitted):
        client.post(URL, json=BODY, headers=_auth())
        again = client.post(URL, json=BODY, headers=_auth())
        assert again.status_code == 202 and again.json()["idempotent"] is True
        assert submitted == ["veille-2026-10-02"]

    def test_another_revision_of_a_live_run_is_a_conflict(self, client, submitted):
        client.post(URL, json=BODY, headers=_auth())
        response = client.post(URL, json={**BODY, "revision": "hash-2"}, headers=_auth())
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VEILLE_REVISION_CONFLICT"
        assert submitted == ["veille-2026-10-02"]

    def test_a_failed_run_can_be_requested_again(self, client, submitted, store):
        client.post(URL, json=BODY, headers=_auth())
        store.by_ref("veille-2026-10-02").update(statut="echec", erreur="x")
        response = client.post(URL, json={**BODY, "revision": "hash-2"}, headers=_auth())
        assert response.status_code == 202 and response.json()["revision"] == "hash-2"
        assert response.json()["statut"] == "accepte"
        assert len(submitted) == 2

    def test_a_new_revision_of_a_finished_veille_restarts_the_production(self, client, submitted, store):
        # Saqr corrige un paragraphe: l'ancien podcast est obsolète, la nouvelle version doit être produite.
        client.post(URL, json=BODY, headers=_auth())
        store.by_ref("veille-2026-10-02").update(statut="pret", audio_url="https://d/vieux")
        response = client.post(URL, json={**BODY, "revision": "hash-2"}, headers=_auth())
        assert response.status_code == 202
        assert response.json()["revision"] == "hash-2" and response.json()["statut"] == "accepte"
        assert "audio_url" not in store.by_ref("veille-2026-10-02")
        assert len(submitted) == 2

    def test_the_same_revision_of_a_finished_veille_is_idempotent(self, client, submitted, store):
        client.post(URL, json=BODY, headers=_auth())
        store.by_ref("veille-2026-10-02").update(statut="pret")
        again = client.post(URL, json=BODY, headers=_auth())
        assert again.status_code == 202 and again.json()["idempotent"] is True and again.json()["statut"] == "pret"
        assert len(submitted) == 1

    def test_a_malformed_ref_is_refused(self, client):
        response = client.post(URL, json={**BODY, "ref": "demain"}, headers=_auth())
        assert response.status_code == 422

    def test_an_unsupported_contract_version_is_refused(self, client):
        response = client.post(URL, json={**BODY, "contractVersion": "9.9"}, headers=_auth())
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CONTRACT_VERSION_UNSUPPORTED"


class TestStatus:
    def test_an_unknown_veille_is_a_404(self, client):
        response = client.get(f"{URL}/veille-2026-01-01", headers=_auth())
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VEILLE_NOT_FOUND"

    def test_a_known_veille_reports_its_state(self, client):
        client.post(URL, json=BODY, headers=_auth())
        body = client.get(f"{URL}/veille-2026-10-02", headers=_auth()).json()
        assert body["statut"] == "accepte" and body["rappel"] == "a_envoyer"
        assert body["audioUrl"] is None and body["pollAfterSeconds"] == 120
