"""Connexions documentaires externes, chiffrees et cloisonnees par tenant.

La facade ne retourne jamais de jeton OAuth. Le tenant est toujours derive du
credential consommateur Qalem ; l'etat OAuth opaque lie ensuite le callback a
cette organisation sans accepter d'identifiant de tenant du navigateur.
"""

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from open_notebook.consumers.errors import (
    CONNECTION_REQUIRED,
    EXTERNAL_SOURCE_NOT_FOUND,
    INVALID_REQUEST,
    OAUTH_STATE_INVALID,
    PROVIDER_UNAVAILABLE,
    ConsumerAPIError,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.utils.encryption import decrypt_value, encrypt_value

GOOGLE_DRIVE = "google-drive"
GOOGLE_DRIVE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/drive.file",
)
GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
NOTION = "notion"
NOTION_API = "https://api.notion.com/v1"
NOTION_AUTHORIZATION_URL = f"{NOTION_API}/oauth/authorize"
NOTION_TOKEN_URL = f"{NOTION_API}/oauth/token"
NOTION_VERSION = "2026-03-11"
_DRIVE_TYPES = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    "application/pdf": ("application/pdf", ".pdf"),
}


@dataclass(frozen=True)
class ExternalDocument:
    external_id: str
    title: str
    provider_version: str
    source_url: Optional[str]
    media_type: str
    filename: str
    payload: bytes
    connection_id: str


def _required_env(name: str, provider: str = GOOGLE_DRIVE) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConsumerAPIError(
            CONNECTION_REQUIRED,
            message="Le fournisseur documentaire n'est pas configure sur Diwan.",
            details={"provider": provider},
        )
    return value


def _callback_url(provider: str) -> str:
    base = _required_env("DIWAN_PUBLIC_URL").rstrip("/")
    if not base.startswith("https://"):
        raise ConsumerAPIError(
            CONNECTION_REQUIRED,
            message="L'URL publique OAuth de Diwan doit utiliser HTTPS.",
            details={"provider": provider},
        )
    return f"{base}/api/v1/consumers/qalem/connectors/{provider}/callback"


def connector_return_url(
    provider: str, status: str, organization_external_id: Optional[str] = None
) -> str:
    base = _required_env("DIWAN_QALEM_RETURN_URL")
    separator = "&" if "?" in base else "?"
    params = {"connector": provider, "status": status}
    if organization_external_id:
        params["orgId"] = organization_external_id
    return f"{base}{separator}{urlencode(params)}"


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def begin_google_drive_authorization(organization_id: str) -> str:
    client_id = _required_env("DIWAN_GOOGLE_DRIVE_CLIENT_ID", GOOGLE_DRIVE)
    state, verifier = await _begin_oauth_state(organization_id, GOOGLE_DRIVE)
    query = {
        "client_id": client_id,
        "redirect_uri": _callback_url(GOOGLE_DRIVE),
        "response_type": "code",
        "scope": " ".join(GOOGLE_DRIVE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode(query)}"


async def _begin_oauth_state(organization_id: str, provider: str) -> Tuple[str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    await repo_query(
        """
        CREATE external_oauth_state SET
            state_hash = $state_hash,
            organization = $organization,
            provider = $provider,
            code_verifier_ciphertext = $verifier,
            expires_at = time::now() + 10m,
            used = false
        """,
        {
            "state_hash": _state_digest(state),
            "organization": ensure_record_id(organization_id),
            "provider": provider,
            "verifier": encrypt_value(verifier),
        },
    )
    return state, verifier


async def begin_notion_authorization(organization_id: str) -> str:
    client_id = _required_env("DIWAN_NOTION_CLIENT_ID", NOTION)
    state, _verifier = await _begin_oauth_state(organization_id, NOTION)
    query = {
        "client_id": client_id,
        "redirect_uri": _callback_url(NOTION),
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    return f"{NOTION_AUTHORIZATION_URL}?{urlencode(query)}"


async def _consume_oauth_state(provider: str, state: str) -> Dict[str, Any]:
    rows = await repo_query(
        """
        UPDATE external_oauth_state SET used = true
        WHERE state_hash = $state_hash
            AND provider = $provider
            AND used = false
            AND expires_at > time::now()
        RETURN BEFORE
        """,
        {"state_hash": _state_digest(state), "provider": provider},
    )
    if len(rows) != 1:
        raise ConsumerAPIError(OAUTH_STATE_INVALID)
    return rows[0]


async def _provider_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_payload: Optional[Dict[str, Any]] = None,
    auth: Optional[Tuple[str, str]] = None,
    provider: str = GOOGLE_DRIVE,
) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                data=data,
                params=params,
                json=json_payload,
                auth=auth,
            )
    except httpx.HTTPError as exc:
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE) from exc
    if response.status_code in (401, 403, 404):
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    if response.status_code >= 400:
        raise ConsumerAPIError(
            PROVIDER_UNAVAILABLE,
            details={"provider": provider, "status": response.status_code},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE) from exc
    if not isinstance(payload, dict):
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE)
    return payload


async def _existing_credentials(
    organization_id: str, provider: str
) -> Optional[Dict[str, Any]]:
    rows = await repo_query(
        """
        SELECT * FROM external_connection
        WHERE organization = $organization AND provider = $provider AND revoked = false
        LIMIT 1
        """,
        {
            "organization": ensure_record_id(organization_id),
            "provider": provider,
        },
    )
    if not rows:
        return None
    row = rows[0]
    try:
        credentials = json.loads(decrypt_value(str(row["credentials_ciphertext"])))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConsumerAPIError(CONNECTION_REQUIRED) from exc
    if not isinstance(credentials, dict):
        raise ConsumerAPIError(CONNECTION_REQUIRED)
    return {"row": row, "credentials": credentials}


async def _store_connection(
    organization_id: str,
    provider: str,
    credentials: Dict[str, Any],
    account_id: Optional[str],
    account_name: Optional[str],
    scopes: List[str],
) -> None:
    existing = await _existing_credentials(organization_id, provider)
    encrypted = encrypt_value(json.dumps(credentials, separators=(",", ":")))
    if existing:
        await repo_query(
            """
            UPDATE $connection SET
                credentials_ciphertext = $credentials,
                provider_account_id = $account_id,
                provider_account_name = $account_name,
                scopes = $scopes,
                revoked = false,
                updated = time::now()
            """,
            {
                "connection": ensure_record_id(str(existing["row"]["id"])),
                "credentials": encrypted,
                "account_id": account_id,
                "account_name": account_name,
                "scopes": scopes,
            },
        )
        return
    await repo_query(
        """
        CREATE external_connection SET
            organization = $organization,
            provider = $provider,
            credentials_ciphertext = $credentials,
            provider_account_id = $account_id,
            provider_account_name = $account_name,
            scopes = $scopes,
            revoked = false
        """,
        {
            "organization": ensure_record_id(organization_id),
            "provider": provider,
            "credentials": encrypted,
            "account_id": account_id,
            "account_name": account_name,
            "scopes": scopes,
        },
    )


async def _organization_external_id(organization_id: str) -> str:
    external_ids = await repo_query(
        "SELECT VALUE external_id FROM $organization LIMIT 1",
        {"organization": ensure_record_id(organization_id)},
    )
    if not external_ids or not str(external_ids[0]).strip():
        raise ConsumerAPIError(CONNECTION_REQUIRED)
    return str(external_ids[0])


async def complete_google_drive_authorization(code: str, state: str) -> str:
    oauth_state = await _consume_oauth_state(GOOGLE_DRIVE, state)
    organization_id = str(oauth_state["organization"])
    verifier = decrypt_value(str(oauth_state["code_verifier_ciphertext"]))
    token = await _provider_json(
        "POST",
        GOOGLE_TOKEN_URL,
        data={
            "client_id": _required_env("DIWAN_GOOGLE_DRIVE_CLIENT_ID"),
            "client_secret": _required_env("DIWAN_GOOGLE_DRIVE_CLIENT_SECRET"),
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": _callback_url(GOOGLE_DRIVE),
        },
    )
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE)
    existing = await _existing_credentials(organization_id, GOOGLE_DRIVE)
    refresh_token = token.get("refresh_token")
    if not refresh_token and existing:
        refresh_token = existing["credentials"].get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsumerAPIError(
            CONNECTION_REQUIRED,
            message="Google n'a pas emis de jeton de renouvellement.",
            details={"provider": GOOGLE_DRIVE},
        )
    credentials: Dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + int(token.get("expires_in") or 3600),
    }
    user = await _provider_json(
        "GET",
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    account_id = str(user.get("sub") or "") or None
    account_name = str(user.get("email") or "") or None
    scopes = str(token.get("scope") or " ".join(GOOGLE_DRIVE_SCOPES)).split()
    await _store_connection(
        organization_id, GOOGLE_DRIVE, credentials, account_id, account_name, scopes
    )
    return await _organization_external_id(organization_id)


def _notion_auth() -> Tuple[str, str]:
    return (
        _required_env("DIWAN_NOTION_CLIENT_ID", NOTION),
        _required_env("DIWAN_NOTION_CLIENT_SECRET", NOTION),
    )


async def complete_notion_authorization(code: str, state: str) -> str:
    oauth_state = await _consume_oauth_state(NOTION, state)
    organization_id = str(oauth_state["organization"])
    token = await _provider_json(
        "POST",
        NOTION_TOKEN_URL,
        headers={"Accept": "application/json"},
        json_payload={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _callback_url(NOTION),
        },
        auth=_notion_auth(),
        provider=NOTION,
    )
    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE, details={"provider": NOTION})
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": NOTION})
    credentials: Dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    expires_in = token.get("expires_in")
    if isinstance(expires_in, int) and expires_in > 0:
        credentials["expires_at"] = int(time.time()) + expires_in
    workspace_id = str(token.get("workspace_id") or "") or None
    workspace_name = str(token.get("workspace_name") or "") or None
    await _store_connection(
        organization_id,
        NOTION,
        credentials,
        workspace_id,
        workspace_name,
        ["read_content"],
    )
    return await _organization_external_id(organization_id)


async def _notion_access_token(organization_id: str) -> Dict[str, str]:
    existing = await _existing_credentials(organization_id, NOTION)
    if not existing:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": NOTION})
    credentials = existing["credentials"]
    access_token = credentials.get("access_token")
    expires_at = credentials.get("expires_at")
    if (
        isinstance(access_token, str)
        and access_token
        and (not isinstance(expires_at, int) or expires_at > int(time.time()) + 60)
    ):
        return {"token": access_token, "connection_id": str(existing["row"]["id"])}
    refresh_token = credentials.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": NOTION})
    refreshed = await _provider_json(
        "POST",
        NOTION_TOKEN_URL,
        headers={"Accept": "application/json"},
        json_payload={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=_notion_auth(),
        provider=NOTION,
    )
    access_token = refreshed.get("access_token")
    next_refresh_token = refreshed.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": NOTION})
    if not isinstance(next_refresh_token, str) or not next_refresh_token:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": NOTION})
    credentials["access_token"] = access_token
    credentials["refresh_token"] = next_refresh_token
    if isinstance(refreshed.get("expires_in"), int):
        credentials["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
    await repo_query(
        "UPDATE $connection SET credentials_ciphertext = $credentials, updated = time::now()",
        {
            "connection": ensure_record_id(str(existing["row"]["id"])),
            "credentials": encrypt_value(
                json.dumps(credentials, separators=(",", ":"))
            ),
        },
    )
    return {"token": access_token, "connection_id": str(existing["row"]["id"])}


def _notion_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _plain_rich_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return "".join(
        str(part.get("plain_text") or "") for part in value if isinstance(part, dict)
    ).strip()


def _notion_page_title(page: Dict[str, Any]) -> str:
    properties = page.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                title = _plain_rich_text(prop.get("title"))
                if title:
                    return title
    return "Page Notion"


async def search_notion(
    organization_id: str,
    query: str,
    *,
    page_size: int = 20,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    auth = await _notion_access_token(organization_id)
    body: Dict[str, Any] = {
        "page_size": max(1, min(page_size, 100)),
        "filter": {"property": "object", "value": "page"},
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
    }
    if query.strip():
        body["query"] = query.strip()
    if page_token:
        body["start_cursor"] = page_token
    payload = await _provider_json(
        "POST",
        f"{NOTION_API}/search",
        headers=_notion_headers(auth["token"]),
        json_payload=body,
        provider=NOTION,
    )
    items = []
    for page in payload.get("results") or []:
        if (
            not isinstance(page, dict)
            or page.get("object") != "page"
            or page.get("archived")
            or page.get("in_trash")
        ):
            continue
        items.append(
            {
                "externalId": page.get("id"),
                "title": _notion_page_title(page),
                "mediaType": "text/plain",
                "modifiedAt": page.get("last_edited_time"),
                "providerVersion": str(page.get("last_edited_time") or ""),
                "sourceUrl": page.get("url"),
                "downloadAllowed": True,
                "size": None,
            }
        )
    return {"items": items, "nextPageToken": payload.get("next_cursor")}


def _notion_block_text(block: Dict[str, Any]) -> str:
    block_type = block.get("type")
    value = block.get(block_type) if isinstance(block_type, str) else None
    if not isinstance(value, dict):
        return ""
    if block_type == "table_row":
        cells = value.get("cells") or []
        return " | ".join(_plain_rich_text(cell) for cell in cells).strip(" |")
    if block_type == "equation":
        return str(value.get("expression") or "").strip()
    if block_type in {"child_page", "child_database"}:
        return str(value.get("title") or "").strip()
    return _plain_rich_text(value.get("rich_text"))


async def _notion_children(token: str, block_id: str, depth: int = 0) -> List[str]:
    if depth > 12:
        raise ConsumerAPIError(INVALID_REQUEST, details={"provider": NOTION})
    lines: List[str] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        payload = await _provider_json(
            "GET",
            f"{NOTION_API}/blocks/{block_id}/children",
            headers=_notion_headers(token),
            params=params,
            provider=NOTION,
        )
        for block in payload.get("results") or []:
            if not isinstance(block, dict):
                continue
            text = _notion_block_text(block)
            if text:
                lines.append(text)
            if block.get("has_children") and isinstance(block.get("id"), str):
                lines.extend(await _notion_children(token, block["id"], depth + 1))
            if len(lines) > 5000:
                raise ConsumerAPIError(INVALID_REQUEST, details={"provider": NOTION})
        cursor = payload.get("next_cursor") if payload.get("has_more") else None
        if not isinstance(cursor, str) or not cursor:
            break
    return lines


async def load_notion_document(
    organization_id: str, external_id: str
) -> ExternalDocument:
    if not external_id.strip() or len(external_id) > 128:
        raise ConsumerAPIError(INVALID_REQUEST)
    auth = await _notion_access_token(organization_id)
    page = await _provider_json(
        "GET",
        f"{NOTION_API}/pages/{external_id}",
        headers=_notion_headers(auth["token"]),
        provider=NOTION,
    )
    if page.get("object") != "page" or page.get("archived") or page.get("in_trash"):
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    title = _notion_page_title(page)
    lines = await _notion_children(auth["token"], external_id)
    payload = (f"# {title}\n\n" + "\n\n".join(lines)).encode("utf-8")
    if len(payload) <= len(title) + 4 or len(payload) > 10 * 1024 * 1024:
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    return ExternalDocument(
        external_id=external_id,
        title=title,
        provider_version=str(page.get("last_edited_time") or "unknown"),
        source_url=page.get("url"),
        media_type="text/plain",
        filename=f"{title}.txt",
        payload=payload,
        connection_id=auth["connection_id"],
    )


async def connection_metadata(organization_id: str) -> List[Dict[str, Any]]:
    rows = await repo_query(
        """
        SELECT id, provider, provider_account_name, scopes, created, updated
        FROM external_connection
        WHERE organization = $organization AND revoked = false
        ORDER BY provider ASC
        """,
        {"organization": ensure_record_id(organization_id)},
    )
    return [
        {
            "connectionId": str(row.get("id")),
            "provider": row.get("provider"),
            "accountLabel": row.get("provider_account_name"),
            "scopes": row.get("scopes") or [],
            "createdAt": str(row.get("created")),
            "updatedAt": str(row.get("updated")),
        }
        for row in rows
    ]


async def revoke_connection(organization_id: str, provider: str) -> None:
    rows = await repo_query(
        """
        UPDATE external_connection SET revoked = true, updated = time::now()
        WHERE organization = $organization AND provider = $provider AND revoked = false
        RETURN AFTER
        """,
        {
            "organization": ensure_record_id(organization_id),
            "provider": provider,
        },
    )
    if not rows:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": provider})


async def _google_access_token(organization_id: str) -> Dict[str, str]:
    existing = await _existing_credentials(organization_id, GOOGLE_DRIVE)
    if not existing:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": GOOGLE_DRIVE})
    credentials = existing["credentials"]
    access_token = credentials.get("access_token")
    if (
        isinstance(access_token, str)
        and int(credentials.get("expires_at") or 0) > int(time.time()) + 60
    ):
        return {"token": access_token, "connection_id": str(existing["row"]["id"])}
    refresh_token = credentials.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": GOOGLE_DRIVE})
    refreshed = await _provider_json(
        "POST",
        GOOGLE_TOKEN_URL,
        data={
            "client_id": _required_env("DIWAN_GOOGLE_DRIVE_CLIENT_ID"),
            "client_secret": _required_env("DIWAN_GOOGLE_DRIVE_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    access_token = refreshed.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsumerAPIError(CONNECTION_REQUIRED, details={"provider": GOOGLE_DRIVE})
    credentials["access_token"] = access_token
    credentials["expires_at"] = int(time.time()) + int(
        refreshed.get("expires_in") or 3600
    )
    await repo_query(
        "UPDATE $connection SET credentials_ciphertext = $credentials, updated = time::now()",
        {
            "connection": ensure_record_id(str(existing["row"]["id"])),
            "credentials": encrypt_value(
                json.dumps(credentials, separators=(",", ":"))
            ),
        },
    )
    return {"token": access_token, "connection_id": str(existing["row"]["id"])}


def _drive_query(query: str) -> str:
    mime_filter = " or ".join(f"mimeType = '{mime}'" for mime in _DRIVE_TYPES)
    clauses = ["trashed = false", f"({mime_filter})"]
    if query.strip():
        escaped = query.strip().replace("\\", "\\\\").replace("'", "\\'")
        clauses.append(f"name contains '{escaped}'")
    return " and ".join(clauses)


async def search_google_drive(
    organization_id: str,
    query: str,
    *,
    page_size: int = 20,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    auth = await _google_access_token(organization_id)
    params: Dict[str, Any] = {
        "q": _drive_query(query),
        "pageSize": max(1, min(page_size, 100)),
        "spaces": "drive",
        "orderBy": "modifiedTime desc",
        "fields": (
            "nextPageToken,files(id,name,mimeType,modifiedTime,version,md5Checksum,"
            "webViewLink,size,capabilities(canDownload))"
        ),
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    payload = await _provider_json(
        "GET",
        f"{GOOGLE_DRIVE_API}/files",
        headers={"Authorization": f"Bearer {auth['token']}"},
        params=params,
    )
    items = []
    for item in payload.get("files") or []:
        if not isinstance(item, dict) or item.get("mimeType") not in _DRIVE_TYPES:
            continue
        raw_size = item.get("size")
        size = (
            int(raw_size)
            if isinstance(raw_size, (str, int)) and str(raw_size).isdigit()
            else None
        )
        items.append(
            {
                "externalId": item.get("id"),
                "title": item.get("name"),
                "mediaType": item.get("mimeType"),
                "modifiedAt": item.get("modifiedTime"),
                "providerVersion": str(
                    item.get("version") or item.get("modifiedTime") or ""
                ),
                "sourceUrl": item.get("webViewLink"),
                "downloadAllowed": bool(
                    (item.get("capabilities") or {}).get("canDownload")
                ),
                "size": size,
            }
        )
    return {"items": items, "nextPageToken": payload.get("nextPageToken")}


async def load_google_drive_document(
    organization_id: str, external_id: str
) -> ExternalDocument:
    if not external_id.strip() or len(external_id) > 512:
        raise ConsumerAPIError(INVALID_REQUEST)
    auth = await _google_access_token(organization_id)
    headers = {"Authorization": f"Bearer {auth['token']}"}
    metadata = await _provider_json(
        "GET",
        f"{GOOGLE_DRIVE_API}/files/{external_id}",
        headers=headers,
        params={
            "fields": (
                "id,name,mimeType,modifiedTime,version,md5Checksum,webViewLink,"
                "capabilities(canDownload)"
            ),
            "supportsAllDrives": "true",
        },
    )
    media_type = str(metadata.get("mimeType") or "")
    if media_type not in _DRIVE_TYPES or not (metadata.get("capabilities") or {}).get(
        "canDownload"
    ):
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    exported_type, extension = _DRIVE_TYPES[media_type]
    if media_type == "application/pdf":
        url = f"{GOOGLE_DRIVE_API}/files/{external_id}"
        params = {"alt": "media", "supportsAllDrives": "true"}
    else:
        url = f"{GOOGLE_DRIVE_API}/files/{external_id}/export"
        params = {"mimeType": exported_type}
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE) from exc
    if response.status_code in (401, 403, 404):
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    if response.status_code >= 400:
        raise ConsumerAPIError(
            PROVIDER_UNAVAILABLE,
            details={"provider": GOOGLE_DRIVE, "status": response.status_code},
        )
    payload = response.content
    if not payload:
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    title = str(metadata.get("name") or external_id)
    provider_version = str(
        metadata.get("version") or metadata.get("modifiedTime") or "unknown"
    )
    return ExternalDocument(
        external_id=external_id,
        title=title,
        provider_version=provider_version,
        source_url=metadata.get("webViewLink"),
        media_type=exported_type,
        filename=f"{title}{extension}",
        payload=payload,
        connection_id=auth["connection_id"],
    )
