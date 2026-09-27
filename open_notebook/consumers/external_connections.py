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
from typing import Any, Dict, List, Optional
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


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConsumerAPIError(
            CONNECTION_REQUIRED,
            message="Le fournisseur documentaire n'est pas configure sur Diwan.",
            details={"provider": GOOGLE_DRIVE},
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
    client_id = _required_env("DIWAN_GOOGLE_DRIVE_CLIENT_ID")
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
            "provider": GOOGLE_DRIVE,
            "verifier": encrypt_value(verifier),
        },
    )
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
) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.request(
                method, url, headers=headers, data=data, params=params
            )
    except httpx.HTTPError as exc:
        raise ConsumerAPIError(PROVIDER_UNAVAILABLE) from exc
    if response.status_code in (401, 403, 404):
        raise ConsumerAPIError(EXTERNAL_SOURCE_NOT_FOUND)
    if response.status_code >= 400:
        raise ConsumerAPIError(
            PROVIDER_UNAVAILABLE,
            details={"provider": GOOGLE_DRIVE, "status": response.status_code},
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
    credentials = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + int(token.get("expires_in") or 3600),
    }
    user = await _provider_json(
        "GET",
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    encrypted = encrypt_value(json.dumps(credentials, separators=(",", ":")))
    account_id = str(user.get("sub") or "") or None
    account_name = str(user.get("email") or "") or None
    scopes = str(token.get("scope") or " ".join(GOOGLE_DRIVE_SCOPES)).split()
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
    else:
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
                "provider": GOOGLE_DRIVE,
                "credentials": encrypted,
                "account_id": account_id,
                "account_name": account_name,
                "scopes": scopes,
            },
        )
    external_ids = await repo_query(
        "SELECT VALUE external_id FROM $organization LIMIT 1",
        {"organization": ensure_record_id(organization_id)},
    )
    if not external_ids or not str(external_ids[0]).strip():
        raise ConsumerAPIError(CONNECTION_REQUIRED)
    return str(external_ids[0])


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
