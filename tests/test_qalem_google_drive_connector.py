"""Contrat Google Drive de la facade Qalem, sans credential reel."""

import hashlib
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from open_notebook.consumers import external_connections as connectors
from open_notebook.consumers.errors import ConsumerAPIError


@pytest.mark.asyncio
async def test_authorization_scope_et_etat_sont_minimaux(monkeypatch):
    writes: List[Dict[str, Any]] = []

    async def fake_repo(query, params):
        writes.append(params)
        return [{"id": "external_oauth_state:1"}]

    monkeypatch.setattr(connectors, "repo_query", fake_repo)
    monkeypatch.setenv("OPEN_NOTEBOOK_ENCRYPTION_KEY", "cle-de-test")
    monkeypatch.setenv("DIWAN_GOOGLE_DRIVE_CLIENT_ID", "client-public")
    monkeypatch.setenv("DIWAN_PUBLIC_URL", "https://diwan.example")

    authorization_url = await connectors.begin_google_drive_authorization(
        "external_organization:tenant-a"
    )
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert query["scope"] == [" ".join(connectors.GOOGLE_DRIVE_SCOPES)]
    assert "drive.readonly" not in query["scope"][0]
    assert "drive.file" in query["scope"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert (
        writes[0]["state_hash"]
        == hashlib.sha256(query["state"][0].encode("utf-8")).hexdigest()
    )
    assert query["state"][0] not in str(writes[0])
    assert writes[0]["organization"].table_name == "external_organization"
    assert "tenant-a" in str(writes[0]["organization"])


@pytest.mark.asyncio
async def test_search_drive_ne_retourne_que_les_formats_contractuels(monkeypatch):
    captured: Dict[str, Any] = {}

    async def fake_token(organization_id):
        assert organization_id == "external_organization:tenant-a"
        return {"token": "secret", "connection_id": "external_connection:1"}

    async def fake_json(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "files": [
                {
                    "id": "doc-1",
                    "name": "Formation SIPOC",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-09-27T10:00:00Z",
                    "version": "7",
                    "webViewLink": "https://docs.google.com/document/d/doc-1",
                    "capabilities": {"canDownload": True},
                },
                {
                    "id": "sheet-1",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                },
            ],
            "nextPageToken": "next",
        }

    monkeypatch.setattr(connectors, "_google_access_token", fake_token)
    monkeypatch.setattr(connectors, "_provider_json", fake_json)

    result = await connectors.search_google_drive(
        "external_organization:tenant-a", "SIPOC", page_size=20
    )

    assert captured["method"] == "GET"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert "name contains 'SIPOC'" in captured["params"]["q"]
    assert len(result["items"]) == 1
    assert result["items"][0]["externalId"] == "doc-1"
    assert result["items"][0]["providerVersion"] == "7"
    assert result["nextPageToken"] == "next"


@pytest.mark.asyncio
async def test_document_interdit_est_refuse_avant_telechargement(monkeypatch):
    async def fake_token(_organization_id):
        return {"token": "secret", "connection_id": "external_connection:1"}

    async def fake_json(_method, _url, **_kwargs):
        return {
            "id": "doc-1",
            "name": "Confidentiel",
            "mimeType": "application/vnd.google-apps.document",
            "capabilities": {"canDownload": False},
        }

    monkeypatch.setattr(connectors, "_google_access_token", fake_token)
    monkeypatch.setattr(connectors, "_provider_json", fake_json)

    with pytest.raises(ConsumerAPIError) as error:
        await connectors.load_google_drive_document(
            "external_organization:tenant-a", "doc-1"
        )
    assert error.value.code == "EXTERNAL_SOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_export_google_doc_epingle_version_et_provenance(monkeypatch):
    async def fake_token(_organization_id):
        return {"token": "secret", "connection_id": "external_connection:1"}

    async def fake_json(_method, _url, **_kwargs):
        return {
            "id": "doc-1",
            "name": "SIPOC",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-09-27T10:00:00Z",
            "version": "12",
            "webViewLink": "https://docs.google.com/document/d/doc-1",
            "capabilities": {"canDownload": True},
        }

    class FakeResponse:
        status_code = 200
        content = b"Fournisseurs, entrees, processus, sorties, clients."

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            assert url.endswith("/files/doc-1/export")
            assert kwargs["params"] == {"mimeType": "text/plain"}
            return FakeResponse()

    monkeypatch.setattr(connectors, "_google_access_token", fake_token)
    monkeypatch.setattr(connectors, "_provider_json", fake_json)
    monkeypatch.setattr(connectors.httpx, "AsyncClient", FakeClient)

    document = await connectors.load_google_drive_document(
        "external_organization:tenant-a", "doc-1"
    )
    assert document.external_id == "doc-1"
    assert document.provider_version == "12"
    assert document.source_url == "https://docs.google.com/document/d/doc-1"
    assert document.media_type == "text/plain"
    assert document.payload.startswith(b"Fournisseurs")


@pytest.mark.asyncio
async def test_erreur_reseau_fournisseur_est_rejouable(monkeypatch):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            raise httpx.ConnectError("indisponible")

    monkeypatch.setattr(connectors.httpx, "AsyncClient", FailingClient)
    with pytest.raises(ConsumerAPIError) as error:
        await connectors._provider_json(
            "GET", "https://www.googleapis.com/drive/v3/files"
        )
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert error.value.retryable is True
