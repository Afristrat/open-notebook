"""Facade documentaire versionnee pour le consommateur Qalem.

Seul contrat que Qalem doit connaitre. Toutes les routes vivent sous
/api/v1/consumers/qalem et sont fermees par defaut: sans jeton valide, rien ne
repond, meme lorsque le middleware historique de Diwan laisse tout passer.

Cette facade n'a AUCUN repli vers les routes historiques /api/sources ou
/api/notebooks, qui restent le domaine interne de Diwan.
"""

import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse

from open_notebook.consumers import CONTRACT_VERSION
from open_notebook.consumers.analysis import analyse_alignment, detect_conflicts
from open_notebook.consumers.auth import (
    ConsumerIdentity,
    authenticate,
    authenticate_for_ingestion,
    require_consumer,
)
from open_notebook.consumers.errors import (
    CONTRACT_VERSION_UNSUPPORTED,
    INVALID_REQUEST,
    SOURCE_NOT_READY,
    ConsumerAPIError,
)
from open_notebook.consumers.external_connections import (
    GOOGLE_DRIVE,
    NOTION,
    ExternalDocument,
    begin_google_drive_authorization,
    begin_notion_authorization,
    complete_google_drive_authorization,
    complete_notion_authorization,
    connection_metadata,
    connector_return_url,
    load_google_drive_document,
    load_notion_document,
    revoke_connection,
    search_google_drive,
    search_notion,
)
from open_notebook.consumers.ingestion import (
    attach_to_corpus,
    max_files_per_request,
    sha256_of,
    store_original,
    validate_upload,
)
from open_notebook.consumers.retrieval import evidence_payload, scoped_search
from open_notebook.consumers.scope import (
    authorized_versions,
    create_corpus,
    get_corpus_or_fail,
    list_corpus_sources,
    resolve_organization,
)
from open_notebook.database.repository import ensure_record_id, repo_query

CONSUMER_ID = "qalem"
POLL_AFTER_SECONDS = 30

router = APIRouter(prefix="/v1/consumers/qalem", tags=["consumers-qalem"])


def _request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if not existing:
        existing = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = existing
    return existing


async def scoped_identity(
    request: Request, identity: ConsumerIdentity = Depends(authenticate)
) -> ConsumerIdentity:
    require_consumer(identity, CONSUMER_ID)
    _request_id(request)
    return identity


async def ingestion_identity(
    request: Request, identity: ConsumerIdentity = Depends(authenticate_for_ingestion)
) -> ConsumerIdentity:
    require_consumer(identity, CONSUMER_ID)
    _request_id(request)
    return identity


def _envelope(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "requestId": _request_id(request),
        **payload,
    }


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _check_contract(version: Optional[str]) -> None:
    if version and version != CONTRACT_VERSION:
        raise ConsumerAPIError(
            CONTRACT_VERSION_UNSUPPORTED, details={"supported": [CONTRACT_VERSION]}
        )


class RetrieveRequest(BaseModel):
    corpusId: str
    sourceIds: List[str] = Field(default_factory=list)
    query: str
    limit: int = 12
    minimumScore: float = 0.35
    searchMode: str = "hybrid"
    contractVersion: Optional[str] = None


class ManifestRequest(BaseModel):
    sourceIds: List[str] = Field(default_factory=list)
    contractVersion: Optional[str] = None


class AlignmentRequest(BaseModel):
    corpusId: str
    sourceIds: List[str] = Field(default_factory=list)
    authorRequest: str
    expectedLanguage: str = "fr-FR"
    contractVersion: Optional[str] = None


class ConflictsRequest(BaseModel):
    corpusId: str
    sourceIds: List[str] = Field(default_factory=list)
    contractVersion: Optional[str] = None


class ConnectorImportRequest(BaseModel):
    externalIds: List[str] = Field(min_length=1, max_length=20)
    corpusId: Optional[str] = None
    corpusName: Optional[str] = None
    idempotencyKey: str = Field(min_length=1, max_length=512)
    contractVersion: Optional[str] = None


@router.get("/connectors")
async def list_connectors(
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Liste les connexions actives sans jamais exposer leurs credentials."""
    organization_id = await resolve_organization(identity)
    return _envelope(
        request, {"connections": await connection_metadata(organization_id)}
    )


@router.post("/connectors/google-drive/authorize")
async def authorize_google_drive(
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Demarre une connexion Google limitee aux fichiers choisis par l'auteur."""
    organization_id = await resolve_organization(identity)
    authorization_url = await begin_google_drive_authorization(organization_id)
    return _envelope(
        request,
        {
            "provider": GOOGLE_DRIVE,
            "authorizationUrl": authorization_url,
            "expiresInSeconds": 600,
        },
    )


@router.get("/connectors/google-drive/callback", include_in_schema=False)
async def google_drive_callback(code: str, state: str) -> RedirectResponse:
    """Callback OAuth public ; l'organisation provient uniquement de l'etat signe."""
    organization_external_id = await complete_google_drive_authorization(code, state)
    return RedirectResponse(
        connector_return_url(GOOGLE_DRIVE, "connected", organization_external_id),
        status_code=303,
    )


@router.get("/connectors/google-drive/search")
async def google_drive_search(
    request: Request,
    query: str = Query(default="", max_length=300),
    pageSize: int = Query(default=20, ge=1, le=100),
    pageToken: Optional[str] = Query(default=None, max_length=2048),
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Recherche uniquement les Docs, Slides et PDF ouverts avec Diwan."""
    organization_id = await resolve_organization(identity)
    result = await search_google_drive(
        organization_id, query, page_size=pageSize, page_token=pageToken
    )
    return _envelope(request, {"provider": GOOGLE_DRIVE, **result})


@router.delete("/connectors/google-drive")
async def disconnect_google_drive(
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Revoque la connexion sans supprimer les versions deja citees."""
    organization_id = await resolve_organization(identity)
    await revoke_connection(organization_id, GOOGLE_DRIVE)
    return _envelope(request, {"provider": GOOGLE_DRIVE, "status": "revoked"})


@router.post("/connectors/notion/authorize")
async def authorize_notion(
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Demarre l'autorisation Notion et son choix explicite de pages."""
    organization_id = await resolve_organization(identity)
    authorization_url = await begin_notion_authorization(organization_id)
    return _envelope(
        request,
        {
            "provider": NOTION,
            "authorizationUrl": authorization_url,
            "expiresInSeconds": 600,
        },
    )


@router.get("/connectors/notion/callback", include_in_schema=False)
async def notion_callback(code: str, state: str) -> RedirectResponse:
    """Termine OAuth Notion sans accepter de tenant depuis le navigateur."""
    organization_external_id = await complete_notion_authorization(code, state)
    return RedirectResponse(
        connector_return_url(NOTION, "connected", organization_external_id),
        status_code=303,
    )


@router.get("/connectors/notion/search")
async def notion_search(
    request: Request,
    query: str = Query(default="", max_length=300),
    pageSize: int = Query(default=20, ge=1, le=100),
    pageToken: Optional[str] = Query(default=None, max_length=2048),
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Recherche les pages que l'auteur a explicitement partagees avec Diwan."""
    organization_id = await resolve_organization(identity)
    result = await search_notion(
        organization_id, query, page_size=pageSize, page_token=pageToken
    )
    return _envelope(request, {"provider": NOTION, **result})


@router.delete("/connectors/notion")
async def disconnect_notion(
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Revoque la connexion Notion sans effacer les versions citees."""
    organization_id = await resolve_organization(identity)
    await revoke_connection(organization_id, NOTION)
    return _envelope(request, {"provider": NOTION, "status": "revoked"})


async def _import_external_documents(
    request: Request,
    payload: ConnectorImportRequest,
    identity: ConsumerIdentity,
    provider: str,
    default_corpus_name: str,
    loader: Callable[[str, str], Awaitable[ExternalDocument]],
) -> Dict[str, Any]:
    """Epingle des versions externes dans le contrat documentaire commun."""
    from surreal_commands import submit_command

    _check_contract(payload.contractVersion)
    organization_id = await resolve_organization(identity)
    existing_job = await repo_query(
        """
        SELECT * FROM ingestion_job
        WHERE organization = $organization AND idempotency_key = $key LIMIT 1
        """,
        {
            "organization": ensure_record_id(organization_id),
            "key": payload.idempotencyKey,
        },
    )
    if existing_job:
        job = existing_job[0]
        return _envelope(
            request,
            {
                "jobId": _as_str(job["id"]),
                "corpusId": _as_str(job.get("corpus")),
                "status": job.get("status", "queued"),
                "submittedSources": job.get("submitted_sources", 0),
                "pollAfterSeconds": POLL_AFTER_SECONDS,
            },
        )

    if payload.corpusId:
        corpus = await get_corpus_or_fail(organization_id, payload.corpusId)
    else:
        corpus = await create_corpus(
            organization_id, (payload.corpusName or default_corpus_name).strip()
        )
    corpus_id = _as_str(corpus["id"])
    documents = [
        await loader(organization_id, external_id)
        for external_id in dict.fromkeys(payload.externalIds)
    ]
    if len(documents) != len(payload.externalIds):
        raise ConsumerAPIError(
            INVALID_REQUEST, message="Chaque identifiant externe doit etre unique."
        )

    job_rows = await repo_query(
        """
        CREATE ingestion_job SET
            consumer = (SELECT VALUE id FROM consumer WHERE name = $consumer LIMIT 1)[0],
            organization = $organization,
            corpus = $corpus,
            idempotency_key = $key,
            status = 'queued',
            submitted_sources = $count,
            request_id = $request_id
        """,
        {
            "consumer": identity.consumer_id,
            "organization": ensure_record_id(organization_id),
            "corpus": ensure_record_id(corpus_id),
            "key": payload.idempotencyKey,
            "count": len(documents),
            "request_id": _request_id(request),
        },
    )
    job_id = _as_str(job_rows[0]["id"])
    queued = 0

    for document in documents:
        media_type = validate_upload(document.filename, document.payload)
        checksum = sha256_of(document.payload)
        stored = store_original(document.payload, checksum, media_type)
        previous = await repo_query(
            """
            SELECT * FROM source_version
            WHERE external_connection = $connection
                AND external_provider = $provider
                AND external_id = $external_id
            ORDER BY version DESC LIMIT 1
            """,
            {
                "connection": ensure_record_id(document.connection_id),
                "provider": provider,
                "external_id": document.external_id,
            },
        )
        reused = next(
            (
                row
                for row in previous
                if row.get("external_version") == document.provider_version
                and row.get("checksum_sha256") == checksum
                and row.get("status") == "ready"
            ),
            None,
        )
        if reused:
            version_id = _as_str(reused["id"])
            item_status = "ready"
        else:
            source_id = _as_str(previous[0].get("source")) if previous else ""
            if not source_id:
                source_rows = await repo_query(
                    "CREATE source SET title = $title RETURN AFTER",
                    {"title": document.title},
                )
                source_id = _as_str(source_rows[0]["id"])
            version_number = int(previous[0].get("version") or 0) + 1 if previous else 1
            version_rows = await repo_query(
                """
                CREATE source_version SET
                    source = $source,
                    version = $version,
                    checksum_sha256 = $checksum,
                    title = $title,
                    original_name = $original_name,
                    media_type = $media_type,
                    source_type = $provider,
                    storage_path = $storage_path,
                    external_connection = $connection,
                    external_provider = $provider,
                    external_id = $external_id,
                    external_version = $external_version,
                    external_url = $external_url,
                    status = 'queued'
                """,
                {
                    "source": ensure_record_id(source_id),
                    "version": version_number,
                    "checksum": checksum,
                    "title": document.title,
                    "original_name": document.filename,
                    "media_type": media_type,
                    "storage_path": stored,
                    "connection": ensure_record_id(document.connection_id),
                    "provider": provider,
                    "external_id": document.external_id,
                    "external_version": document.provider_version,
                    "external_url": document.source_url,
                },
            )
            version_id = _as_str(version_rows[0]["id"])
            item_status = "queued"
            queued += 1
        await attach_to_corpus(corpus_id, version_id)
        item_rows = await repo_query(
            """
            CREATE ingestion_item SET
                job = $job,
                source_version = $version,
                original_name = $original_name,
                source_type = $provider,
                payload_ref = $payload_ref,
                status = $status
            """,
            {
                "job": ensure_record_id(job_id),
                "version": ensure_record_id(version_id),
                "original_name": document.filename,
                "provider": provider,
                "payload_ref": stored,
                "status": item_status,
            },
        )
        if item_status == "queued":
            submit_command(
                "open_notebook",
                "consumer_ingest_source",
                {
                    "version_id": version_id,
                    "item_id": _as_str(item_rows[0]["id"]),
                    "source_type": "upload",
                    "payload_ref": stored,
                    "title": document.title,
                },
            )

    status = "queued"
    if queued == 0:
        status = "ready"
        await repo_query(
            "UPDATE $job SET status = 'ready', updated = time::now()",
            {"job": ensure_record_id(job_id)},
        )
    return _envelope(
        request,
        {
            "jobId": job_id,
            "corpusId": corpus_id,
            "status": status,
            "submittedSources": len(documents),
            "pollAfterSeconds": POLL_AFTER_SECONDS,
        },
    )


@router.post("/connectors/google-drive/imports", status_code=202)
async def import_google_drive(
    request: Request,
    payload: ConnectorImportRequest,
    identity: ConsumerIdentity = Depends(ingestion_identity),
) -> Dict[str, Any]:
    """Epingle les versions Google Drive choisies dans un corpus Diwan."""
    return await _import_external_documents(
        request,
        payload,
        identity,
        GOOGLE_DRIVE,
        "Google Drive",
        load_google_drive_document,
    )


@router.post("/connectors/notion/imports", status_code=202)
async def import_notion(
    request: Request,
    payload: ConnectorImportRequest,
    identity: ConsumerIdentity = Depends(ingestion_identity),
) -> Dict[str, Any]:
    """Epingle les versions Notion choisies dans un corpus Diwan."""
    return await _import_external_documents(
        request, payload, identity, NOTION, "Notion", load_notion_document
    )


@router.post("/ingestions", status_code=202)
async def create_ingestion(
    request: Request,
    identity: ConsumerIdentity = Depends(ingestion_identity),
    files: List[UploadFile] = File(default_factory=list),
    urls: List[str] = Form(default_factory=list),
    texts: List[str] = Form(default_factory=list),
    titles: List[str] = Form(default_factory=list),
    corpusId: Optional[str] = Form(default=None),
    corpusName: Optional[str] = Form(default=None),
    idempotencyKey: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    """Importe une ou plusieurs sources dans un corpus, de facon asynchrone."""
    from surreal_commands import submit_command

    idempotency_key = (
        idempotencyKey or request.headers.get("Idempotency-Key") or ""
    ).strip()
    if not idempotency_key:
        raise ConsumerAPIError(
            INVALID_REQUEST,
            message="Une cle d'idempotence est requise pour tout import.",
        )

    if len(files) > max_files_per_request():
        raise ConsumerAPIError(
            INVALID_REQUEST,
            message="Le nombre de fichiers depasse la limite autorisee.",
            details={"maxFiles": max_files_per_request()},
        )

    organization_id = await resolve_organization(identity)

    # Idempotence: une meme cle ne cree jamais deux imports.
    existing = await repo_query(
        """
        SELECT * FROM ingestion_job
        WHERE organization = $organization AND idempotency_key = $key LIMIT 1
        """,
        {
            "organization": ensure_record_id(organization_id),
            "key": idempotency_key,
        },
    )
    if existing:
        job = existing[0]
        return _envelope(
            request,
            {
                "jobId": _as_str(job["id"]),
                "corpusId": _as_str(job.get("corpus")),
                "status": job.get("status", "queued"),
                "submittedSources": job.get("submitted_sources", 0),
                "pollAfterSeconds": POLL_AFTER_SECONDS,
            },
        )

    if corpusId:
        corpus = await get_corpus_or_fail(organization_id, corpusId)
    else:
        corpus = await create_corpus(
            organization_id, (corpusName or "Corpus Qalem").strip()
        )
    corpus_ref = _as_str(corpus["id"])

    payloads: List[Dict[str, Any]] = []
    for index, upload in enumerate(files):
        raw = await upload.read()
        media_type = validate_upload(upload.filename or "", raw)
        checksum = sha256_of(raw)
        stored = store_original(raw, checksum, media_type)
        payloads.append(
            {
                "source_type": "upload",
                "original_name": upload.filename or f"document-{index + 1}",
                "media_type": media_type,
                "checksum": checksum,
                "payload_ref": stored,
                "title": titles[index] if index < len(titles) else None,
            }
        )
    for url in urls:
        if not url.strip():
            continue
        payloads.append(
            {
                "source_type": "url",
                "original_name": url.strip()[:200],
                "media_type": "text/html",
                "checksum": sha256_of(url.strip().encode("utf-8")),
                "url": url.strip(),
            }
        )
    for text in texts:
        if not text.strip():
            continue
        payloads.append(
            {
                "source_type": "text",
                "original_name": "texte",
                "media_type": "text/plain",
                "checksum": sha256_of(text.strip().encode("utf-8")),
                "text": text.strip(),
            }
        )

    if not payloads:
        raise ConsumerAPIError(
            INVALID_REQUEST, message="Aucune source n'a ete fournie dans cet import."
        )

    job_rows = await repo_query(
        """
        CREATE ingestion_job SET
            consumer = (SELECT VALUE id FROM consumer WHERE name = $consumer LIMIT 1)[0],
            organization = $organization,
            corpus = $corpus,
            idempotency_key = $key,
            status = 'queued',
            submitted_sources = $count,
            request_id = $request_id
        """,
        {
            "consumer": identity.consumer_id,
            "organization": ensure_record_id(organization_id),
            "corpus": ensure_record_id(corpus_ref),
            "key": idempotency_key,
            "count": len(payloads),
            "request_id": _request_id(request),
        },
    )
    job_id = _as_str(job_rows[0]["id"])

    for payload in payloads:
        version_rows = await repo_query(
            """
            CREATE source_version SET
                source = (CREATE source SET title = $title RETURN VALUE id)[0],
                version = 1,
                checksum_sha256 = $checksum,
                title = $title,
                original_name = $original_name,
                media_type = $media_type,
                source_type = $source_type,
                storage_path = $storage_path,
                status = 'queued'
            """,
            {
                "checksum": payload["checksum"],
                "title": payload.get("title") or payload["original_name"],
                "original_name": payload["original_name"],
                "media_type": payload["media_type"],
                "source_type": payload["source_type"],
                "storage_path": payload.get("payload_ref"),
            },
        )
        version_id = _as_str(version_rows[0]["id"])
        await attach_to_corpus(corpus_ref, version_id)

        item_rows = await repo_query(
            """
            CREATE ingestion_item SET
                job = $job,
                source_version = $version,
                original_name = $original_name,
                source_type = $source_type,
                payload_ref = $payload_ref,
                status = 'queued'
            """,
            {
                "job": ensure_record_id(job_id),
                "version": ensure_record_id(version_id),
                "original_name": payload["original_name"],
                "source_type": payload["source_type"],
                "payload_ref": payload.get("payload_ref"),
            },
        )
        item_id = _as_str(item_rows[0]["id"])

        submit_command(
            "open_notebook",
            "consumer_ingest_source",
            {
                "version_id": version_id,
                "item_id": item_id,
                "source_type": payload["source_type"],
                "payload_ref": payload.get("payload_ref"),
                "url": payload.get("url"),
                "text": payload.get("text"),
                "title": payload.get("title"),
            },
        )

    logger.info(
        f"[consumers] import accepte consumer={identity.consumer_id} "
        f"organization={organization_id} corpus={corpus_ref} job={job_id} "
        f"sources={len(payloads)}"
    )

    return _envelope(
        request,
        {
            "jobId": job_id,
            "corpusId": corpus_ref,
            "status": "queued",
            "submittedSources": len(payloads),
            "pollAfterSeconds": POLL_AFTER_SECONDS,
        },
    )


@router.get("/ingestions/{job_id:path}")
async def get_ingestion(
    request: Request,
    job_id: str,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Suivi d'un import: le serveur reste la source de verite."""
    organization_id = await resolve_organization(identity)
    jobs = await repo_query(
        "SELECT * FROM $job WHERE organization = $organization LIMIT 1",
        {
            "job": ensure_record_id(job_id),
            "organization": ensure_record_id(organization_id),
        },
    )
    if not jobs:
        raise ConsumerAPIError(
            "CORPUS_NOT_FOUND",
            message="Cet import est introuvable dans ce perimetre.",
            details={"jobId": job_id},
        )
    job = jobs[0]

    # SurrealDB exige que le champ du ORDER BY figure dans la projection:
    # sans `created` selectionne, la requete echoue avec "Missing order idiom".
    items = await repo_query(
        """
        SELECT
            id,
            created,
            status,
            error_code,
            error_message,
            original_name,
            source_version.id AS version_id,
            source_version.source AS source_id,
            source_version.checksum_sha256 AS checksum,
            source_version.pages AS pages,
            source_version.chunks AS chunks
        FROM ingestion_item WHERE job = $job ORDER BY created ASC
        """,
        {"job": ensure_record_id(job_id)},
    )

    sources = [
        {
            "sourceId": _as_str(item.get("source_id")),
            "sourceVersion": _as_str(item.get("version_id")),
            "originalName": item.get("original_name"),
            "status": item.get("status"),
            "checksumSha256": f"sha256:{item['checksum']}"
            if item.get("checksum")
            else None,
            "pages": item.get("pages"),
            "chunks": item.get("chunks") or 0,
            "errorCode": item.get("error_code"),
            "errorMessage": item.get("error_message"),
        }
        for item in items
    ]

    done = sum(1 for s in sources if s["status"] in ("ready", "failed"))
    progress = int((done / len(sources)) * 100) if sources else 0

    return _envelope(
        request,
        {
            "jobId": job_id,
            "corpusId": _as_str(job.get("corpus")),
            "status": job.get("status", "queued"),
            "progress": progress,
            "sources": sources,
            "pollAfterSeconds": POLL_AFTER_SECONDS,
        },
    )


@router.get("/sources")
async def list_sources(
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
    corpusId: Optional[str] = None,
    status: Optional[str] = None,
    query: Optional[str] = None,
    mediaType: Optional[str] = None,
    page: int = 1,
    pageSize: int = 20,
) -> Dict[str, Any]:
    """Bibliotheque documentaire limitee a l'organisation du jeton."""
    organization_id = await resolve_organization(identity)
    if corpusId:
        await get_corpus_or_fail(organization_id, corpusId)

    rows, total = await list_corpus_sources(
        organization_id,
        corpus_id=corpusId,
        status=status,
        query=query,
        media_type=mediaType,
        page=page,
        page_size=pageSize,
    )

    return _envelope(
        request,
        {
            "items": [
                {
                    "sourceId": _as_str(row.get("source_id")),
                    "sourceVersion": _as_str(row.get("version_id")),
                    "corpusId": _as_str(row.get("corpus_id")),
                    "title": row.get("title"),
                    "originalName": row.get("original_name"),
                    "mediaType": row.get("media_type"),
                    "status": row.get("status"),
                    "checksumSha256": f"sha256:{row['checksum_sha256']}"
                    if row.get("checksum_sha256")
                    else None,
                    "pages": row.get("pages"),
                    "chunks": row.get("chunks") or 0,
                    "createdAt": _as_str(row.get("created")),
                }
                for row in rows
            ],
            "pagination": {
                "page": max(1, page),
                "pageSize": max(1, min(pageSize, 100)),
                "total": total,
            },
        },
    )


@router.post("/sources/manifest")
async def sources_manifest(
    request: Request,
    payload: ManifestRequest,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Manifeste verifiable des sources demandees."""
    _check_contract(payload.contractVersion)
    organization_id = await resolve_organization(identity)
    versions = await authorized_versions(organization_id, payload.sourceIds)

    return _envelope(
        request,
        {
            "sources": [
                {
                    "sourceId": _as_str(v.get("source_id")),
                    "sourceVersion": _as_str(v.get("version_id")),
                    "title": v.get("title"),
                    "checksumSha256": f"sha256:{v['checksum_sha256']}"
                    if v.get("checksum_sha256")
                    else None,
                    "status": v.get("status"),
                    "parser": {
                        "name": v.get("parser_name"),
                        "version": v.get("parser_version"),
                    },
                    "embedding": {
                        "model": v.get("embedding_model"),
                        "version": v.get("embedding_model_version"),
                        "chunks": v.get("chunks") or 0,
                    },
                }
                for v in versions
            ]
        },
    )


async def _ready_versions(
    organization_id: str, corpus_id: str, source_ids: List[str]
) -> List[Dict[str, Any]]:
    """Valide le perimetre puis exige que les sources soient exploitables."""
    if not source_ids:
        raise ConsumerAPIError(
            INVALID_REQUEST,
            message="La liste des sources est obligatoire: cette facade n'autorise "
            "aucune recherche globale.",
        )
    await get_corpus_or_fail(organization_id, corpus_id)
    versions = await authorized_versions(
        organization_id, source_ids, corpus_id=corpus_id
    )
    not_ready = [
        _as_str(v.get("source_id")) for v in versions if v.get("status") != "ready"
    ]
    if not_ready:
        raise ConsumerAPIError(SOURCE_NOT_READY, details={"sourceIds": not_ready})
    return versions


@router.post("/retrieve")
async def retrieve(
    request: Request,
    payload: RetrieveRequest,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Recherche strictement limitee a une liste blanche de sources."""
    _check_contract(payload.contractVersion)
    organization_id = await resolve_organization(identity)
    versions = await _ready_versions(
        organization_id, payload.corpusId, payload.sourceIds
    )
    index = {_as_str(v["version_id"]): v for v in versions}

    rows = await scoped_search(
        list(index.keys()),
        payload.query,
        limit=payload.limit,
        minimum_score=payload.minimumScore,
        search_mode=payload.searchMode,
    )
    evidence = [evidence_payload(row, index) for row in rows]

    logger.info(
        f"[consumers] recherche consumer={identity.consumer_id} "
        f"corpus={payload.corpusId} sources={len(payload.sourceIds)} "
        f"resultats={len(evidence)}"
    )

    return _envelope(
        request,
        {
            "status": "ok" if evidence else "insufficient_evidence",
            "query": payload.query,
            "evidence": evidence,
        },
    )


@router.post("/alignment")
async def alignment(
    request: Request,
    payload: AlignmentRequest,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Compatibilite entre une demande de formation et les sources autorisees."""
    _check_contract(payload.contractVersion)
    organization_id = await resolve_organization(identity)
    versions = await _ready_versions(
        organization_id, payload.corpusId, payload.sourceIds
    )
    index = {_as_str(v["version_id"]): v for v in versions}

    rows = await scoped_search(
        list(index.keys()),
        payload.authorRequest,
        limit=24,
        minimum_score=0.2,
        search_mode="hybrid",
    )
    evidence = [evidence_payload(row, index) for row in rows]

    result = await analyse_alignment(
        payload.authorRequest, evidence, expected_language=payload.expectedLanguage
    )
    return _envelope(request, result)


@router.post("/conflicts")
async def conflicts(
    request: Request,
    payload: ConflictsRequest,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Contradictions substantielles entre les sources d'un corpus."""
    _check_contract(payload.contractVersion)
    organization_id = await resolve_organization(identity)
    versions = await _ready_versions(
        organization_id, payload.corpusId, payload.sourceIds
    )
    index = {_as_str(v["version_id"]): v for v in versions}

    rows = await scoped_search(
        list(index.keys()),
        "affirmations principales, definitions, chiffres, recommandations",
        limit=30,
        minimum_score=0.15,
        search_mode="hybrid",
    )
    evidence = [evidence_payload(row, index) for row in rows]

    status, findings = await detect_conflicts(evidence)
    return _envelope(request, {"status": status, "conflicts": findings})


@router.delete("/corpora/{corpus_id:path}")
async def revoke_corpus(
    request: Request,
    corpus_id: str,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    """Revoque l'acces a un corpus sans supprimer les sources partagees.

    Le rattachement est marque revoque, jamais efface: une source presente dans
    un autre corpus y reste intacte, et l'historique de provenance survit.
    """
    organization_id = await resolve_organization(identity)
    await get_corpus_or_fail(organization_id, corpus_id)

    await repo_query(
        "UPDATE corpus_source SET revoked = true, updated = time::now() "
        "WHERE corpus = $corpus",
        {"corpus": ensure_record_id(corpus_id)},
    )
    await repo_query(
        "UPDATE $corpus SET revoked = true, updated = time::now()",
        {"corpus": ensure_record_id(corpus_id)},
    )

    logger.info(
        f"[consumers] corpus revoque consumer={identity.consumer_id} corpus={corpus_id}"
    )
    return _envelope(request, {"corpusId": corpus_id, "status": "revoked"})
