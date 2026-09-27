"""Contrat Notion de la facade Qalem, sans credential reel."""

import hashlib
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

from open_notebook.consumers import external_connections as connectors


@pytest.mark.asyncio
async def test_authorization_notion_est_tenant_scopee(monkeypatch):
    writes: List[Dict[str, Any]] = []

    async def fake_repo(_query, params):
        writes.append(params)
        return [{"id": "external_oauth_state:notion-1"}]

    monkeypatch.setattr(connectors, "repo_query", fake_repo)
    monkeypatch.setenv("OPEN_NOTEBOOK_ENCRYPTION_KEY", "cle-de-test")
    monkeypatch.setenv("DIWAN_NOTION_CLIENT_ID", "notion-client-public")
    monkeypatch.setenv("DIWAN_PUBLIC_URL", "https://diwan.example")

    authorization_url = await connectors.begin_notion_authorization(
        "external_organization:tenant-a"
    )
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://api.notion.com/v1/oauth/authorize"
    )
    assert query["client_id"] == ["notion-client-public"]
    assert query["owner"] == ["user"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [
        "https://diwan.example/api/v1/consumers/qalem/connectors/notion/callback"
    ]
    assert writes[0]["provider"] == connectors.NOTION
    assert (
        writes[0]["state_hash"]
        == hashlib.sha256(query["state"][0].encode("utf-8")).hexdigest()
    )
    assert query["state"][0] not in str(writes[0])


@pytest.mark.asyncio
async def test_search_notion_ne_retourne_que_les_pages_partagees(monkeypatch):
    captured: Dict[str, Any] = {}

    async def fake_token(organization_id):
        assert organization_id == "external_organization:tenant-a"
        return {"token": "secret", "connection_id": "external_connection:2"}

    async def fake_json(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return {
            "results": [
                {
                    "object": "page",
                    "id": "page-1",
                    "url": "https://notion.so/page-1",
                    "last_edited_time": "2026-09-27T10:00:00Z",
                    "archived": False,
                    "in_trash": False,
                    "properties": {
                        "Nom": {
                            "type": "title",
                            "title": [{"plain_text": "Formation SIPOC"}],
                        }
                    },
                },
                {"object": "database", "id": "database-1"},
                {"object": "page", "id": "page-2", "in_trash": True},
            ],
            "has_more": True,
            "next_cursor": "next",
        }

    monkeypatch.setattr(connectors, "_notion_access_token", fake_token)
    monkeypatch.setattr(connectors, "_provider_json", fake_json)

    result = await connectors.search_notion(
        "external_organization:tenant-a", "SIPOC", page_size=20
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.notion.com/v1/search"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["Notion-Version"] == connectors.NOTION_VERSION
    assert captured["json_payload"]["query"] == "SIPOC"
    assert captured["json_payload"]["filter"] == {
        "property": "object",
        "value": "page",
    }
    assert result["items"] == [
        {
            "externalId": "page-1",
            "title": "Formation SIPOC",
            "mediaType": "text/plain",
            "modifiedAt": "2026-09-27T10:00:00Z",
            "providerVersion": "2026-09-27T10:00:00Z",
            "sourceUrl": "https://notion.so/page-1",
            "downloadAllowed": True,
            "size": None,
        }
    ]
    assert result["nextPageToken"] == "next"


@pytest.mark.asyncio
async def test_page_notion_est_lue_avec_ses_blocs_enfants(monkeypatch):
    async def fake_token(_organization_id):
        return {"token": "secret", "connection_id": "external_connection:2"}

    async def fake_json(_method, url, **_kwargs):
        if "/pages/" in url:
            return {
                "object": "page",
                "id": "page-1",
                "url": "https://notion.so/page-1",
                "last_edited_time": "2026-09-27T10:00:00Z",
                "archived": False,
                "in_trash": False,
                "properties": {
                    "Nom": {
                        "type": "title",
                        "title": [{"plain_text": "SIPOC"}],
                    }
                },
            }
        if url.endswith("/blocks/page-1/children"):
            return {
                "results": [
                    {
                        "id": "block-1",
                        "type": "heading_1",
                        "has_children": False,
                        "heading_1": {"rich_text": [{"plain_text": "Processus"}]},
                    },
                    {
                        "id": "block-2",
                        "type": "toggle",
                        "has_children": True,
                        "toggle": {"rich_text": [{"plain_text": "Détails"}]},
                    },
                ],
                "has_more": False,
            }
        assert url.endswith("/blocks/block-2/children")
        return {
            "results": [
                {
                    "id": "block-3",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {
                        "rich_text": [{"plain_text": "Fournisseurs et clients"}]
                    },
                }
            ],
            "has_more": False,
        }

    monkeypatch.setattr(connectors, "_notion_access_token", fake_token)
    monkeypatch.setattr(connectors, "_provider_json", fake_json)

    document = await connectors.load_notion_document(
        "external_organization:tenant-a", "page-1"
    )

    assert document.external_id == "page-1"
    assert document.provider_version == "2026-09-27T10:00:00Z"
    assert document.filename == "SIPOC.txt"
    assert document.connection_id == "external_connection:2"
    assert document.payload.decode("utf-8") == (
        "# SIPOC\n\nProcessus\n\nDétails\n\nFournisseurs et clients"
    )


def test_blocs_notion_couvrent_table_equation_et_page_enfant():
    assert (
        connectors._notion_block_text(
            {"type": "table_row", "table_row": {"cells": [[{"plain_text": "A"}], []]}}
        )
        == "A"
    )
    assert (
        connectors._notion_block_text(
            {"type": "equation", "equation": {"expression": "x^2"}}
        )
        == "x^2"
    )
    assert (
        connectors._notion_block_text(
            {"type": "child_page", "child_page": {"title": "Annexe"}}
        )
        == "Annexe"
    )
