"""Contrat de Saqr vers Diwan: demander le podcast d'une veille publiée, en suivre l'avancement.

Routes sous /api/v1/consumers/saqr, fermées par défaut comme celles de Qalem: sans jeton valide
(empreinte `saqr:<organisation>:<sha256>` dans DIWAN_CONSUMER_TOKENS), rien ne répond.

POST /veille-podcast répond 202 aussitôt. La production tourne dans la file durable; Saqr est
rappelé à la fin (« pret » ou « echec », jamais le silence) et peut aussi interroger le suivi.
Idempotent sur (ref, revision): même demande, même réponse. Même ref avec une autre révision:
409, sauf si l'essai précédent a échoué (alors la demande relance la production).
"""

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from loguru import logger
from pydantic import BaseModel, Field

from open_notebook.consumers import CONTRACT_VERSION
from open_notebook.consumers.auth import (
    ConsumerIdentity,
    authenticate,
    authenticate_for_ingestion,
    require_consumer,
)
from open_notebook.consumers.errors import (
    CONTRACT_VERSION_UNSUPPORTED,
    VEILLE_NOT_FOUND,
    VEILLE_REVISION_CONFLICT,
    ConsumerAPIError,
)
from open_notebook.veille import pipeline, runs

CONSUMER_ID = "saqr"
POLL_AFTER_SECONDS = 120

router = APIRouter(prefix="/v1/consumers/saqr", tags=["consumers-saqr"])


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
    return {"contractVersion": CONTRACT_VERSION, "requestId": _request_id(request), **payload}


class VeillePodcastRequest(BaseModel):
    ref: str = Field(pattern=r"^veille-\d{4}-\d{2}-\d{2}$")
    revision: str = Field(min_length=1, max_length=128)
    titre: Optional[str] = Field(default=None, max_length=300)
    date_publication: Optional[str] = Field(default=None, max_length=32)
    url: Optional[str] = Field(default=None, max_length=1000)
    contractVersion: Optional[str] = None


@router.post("/veille-podcast", status_code=202)
async def request_veille_podcast(
    body: VeillePodcastRequest,
    request: Request,
    identity: ConsumerIdentity = Depends(ingestion_identity),
) -> Dict[str, Any]:
    if body.contractVersion and body.contractVersion != CONTRACT_VERSION:
        raise ConsumerAPIError(
            CONTRACT_VERSION_UNSUPPORTED, details={"supported": [CONTRACT_VERSION]}
        )

    run = await runs.get_run(body.ref)
    if run is None:
        try:
            run = await runs.create_run(
                body.ref, body.revision, body.titre, body.date_publication, body.url
            )
        except Exception:
            # Deux demandes simultanées: l'index unique sur ref a refusé la seconde.
            run = await runs.get_run(body.ref)
            if run is None:
                raise
            return _accepted(request, run, idempotent=True)
    elif run["statut"] in runs.ACTIVE and run["revision"] != body.revision:
        # Dīwān travaille sur une autre version: Saqr ne compte pas l'essai et retente au passage suivant.
        raise ConsumerAPIError(
            VEILLE_REVISION_CONFLICT, details={"ref": body.ref, "revision": run["revision"]}
        )
    elif run["statut"] == "echec" or run["revision"] != body.revision:
        # Essai précédent échoué (relançable), ou nouvelle version d'une veille déjà terminée:
        # l'ancien podcast est obsolète, on repart sur la version demandée.
        await runs.relaunch_run(
            str(run["id"]), body.revision, body.titre, body.date_publication, body.url
        )
        run = await runs.get_run(body.ref)
    else:
        return _accepted(request, run, idempotent=True)

    assert run is not None
    # Si la soumission échoue, la ligne reste « accepte » sans battement: reconcile la reprend.
    job = pipeline.submit_orchestrator(body.ref)
    await runs.update_run(str(run["id"]), job=job)
    logger.info(f"[veille] demande acceptée pour {body.ref} (job {job})")
    return _accepted(request, run, idempotent=False)


def _accepted(request: Request, run: Dict[str, Any], *, idempotent: bool) -> Dict[str, Any]:
    return _envelope(
        request,
        {**runs.public_view(run), "idempotent": idempotent, "pollAfterSeconds": POLL_AFTER_SECONDS},
    )


@router.get("/veille-podcast/{ref}")
async def get_veille_podcast(
    ref: str,
    request: Request,
    identity: ConsumerIdentity = Depends(scoped_identity),
) -> Dict[str, Any]:
    run = await runs.get_run(ref)
    if run is None:
        raise ConsumerAPIError(VEILLE_NOT_FOUND, details={"ref": ref})
    return _envelope(request, {**runs.public_view(run), "pollAfterSeconds": POLL_AFTER_SECONDS})
