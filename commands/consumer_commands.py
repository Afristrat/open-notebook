"""Traitement asynchrone des imports de la facade consommateur.

La commande vit dans la meme file durable que le reste de Diwan
(surreal-commands, table `command` en base), donc un job survit au redemarrage
du service et reprend sans intervention. Aucun second orchestrateur n'est
introduit.

Difference avec embed_source_command: ici les blocs sont ecrits AVEC leur
provenance et rattaches a une version immuable. Rien n'est supprime, jamais:
une nouvelle version cohabite avec l'ancienne, dont les preuves restent
citables.
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.ai.models import model_manager
from open_notebook.consumers.ingestion import build_chunk_records
from open_notebook.database.repository import (
    ensure_record_id,
    repo_insert,
    repo_query,
)
from open_notebook.domain.notebook import Asset, Source
from open_notebook.graphs.source import content_process
from open_notebook.utils.chunking import chunk_text, detect_content_type
from open_notebook.utils.embedding import generate_embeddings


def _bounded(env_name: str, default: int) -> asyncio.Semaphore:
    raw = os.getenv(env_name)
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return asyncio.Semaphore(max(1, value))


# Concurrences bornees et SEPAREES: l'extraction est gourmande en processeur
# (docling, OCR), la vectorisation sature le service d'embeddings. Les melanger
# derriere une seule limite ferait que la phase la plus lente affame l'autre.
_EXTRACTION_LIMIT = _bounded("DIWAN_CONSUMER_EXTRACTION_CONCURRENCY", 2)
_EMBEDDING_LIMIT = _bounded("DIWAN_CONSUMER_EMBEDDING_CONCURRENCY", 2)

# Verrou par version: empeche deux vectorisations concurrentes du meme document
# lorsqu'un client resoumet un import pendant que le premier tourne encore.
_VERSION_LOCKS: Dict[str, asyncio.Lock] = {}


def _version_lock(version_id: str) -> asyncio.Lock:
    lock = _VERSION_LOCKS.get(version_id)
    if lock is None:
        lock = asyncio.Lock()
        _VERSION_LOCKS[version_id] = lock
    return lock


class ConsumerIngestInput(CommandInput):
    version_id: str
    item_id: str
    source_type: str
    payload_ref: Optional[str] = None
    url: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None


class ConsumerIngestOutput(CommandOutput):
    success: bool
    version_id: str
    chunks_created: int = 0
    processing_time: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def _sanitise(message: str) -> str:
    """Reduit un message d'erreur a une phrase actionnable et non sensible.

    Les exceptions d'extraction citent volontiers le chemin du fichier ou un
    extrait du document. Ni l'un ni l'autre ne doit sortir de Diwan.
    """
    first_line = (message or "").strip().splitlines()[0] if message else ""
    cleaned = first_line.replace("\\", "/")
    cleaned = " ".join(part for part in cleaned.split() if "/" not in part)
    return (cleaned or "Le traitement a echoue.")[:300]


async def _set_status(
    version_id: str,
    item_id: str,
    status: str,
    *,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "updated": "time::now()",
    }
    assignments = "status = $status, error_code = $error_code, error_message = $error_message, updated = time::now()"
    params: Dict[str, Any] = {
        "version": ensure_record_id(version_id),
        "item": ensure_record_id(item_id),
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
    }
    for key, value in (extra or {}).items():
        assignments += f", {key} = ${key}"
        params[key] = value
    del payload
    await repo_query(f"UPDATE $version SET {assignments}", params)
    await repo_query(
        "UPDATE $item SET status = $status, error_code = $error_code, "
        "error_message = $error_message, updated = time::now()",
        {
            "item": params["item"],
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
        },
    )
    await _refresh_job_status(item_id)


async def _refresh_job_status(item_id: str) -> None:
    """Recalcule l'etat agrege du job a partir de ses items.

    Un echec isole ne masque jamais l'etat des autres sources du meme lot: le
    job devient partially_failed, et chaque item garde son propre statut.
    """
    rows = await repo_query(
        "SELECT job FROM $item LIMIT 1", {"item": ensure_record_id(item_id)}
    )
    if not rows:
        return
    raw_job = rows[0].get("job")
    if raw_job is None:
        return
    # repo_query rend les references sous forme de chaines (parse_record_ids):
    # les repasser telles quelles a un UPDATE ne cible aucun enregistrement, et
    # SurrealDB ne signale rien. D'ou un job fige sur "queued" alors que ses
    # items progressaient. Toute reference relue doit etre reconvertie.
    job_ref = ensure_record_id(str(raw_job))

    items = await repo_query(
        "SELECT VALUE status FROM ingestion_item WHERE job = $job", {"job": job_ref}
    )
    statuses = [str(s) for s in items]
    if not statuses:
        return

    total = len(statuses)
    ready = sum(1 for s in statuses if s == "ready")
    failed = sum(1 for s in statuses if s == "failed")

    if ready == total:
        job_status = "ready"
    elif ready + failed == total:
        job_status = "failed" if ready == 0 else "partially_failed"
    elif any(s == "embedding" for s in statuses):
        job_status = "embedding"
    elif any(s == "chunking" for s in statuses):
        job_status = "chunking"
    elif any(s == "extracting" for s in statuses):
        job_status = "extracting"
    else:
        job_status = "queued"

    await repo_query(
        "UPDATE $job SET status = $status, updated = time::now()",
        {"job": job_ref, "status": job_status},
    )


async def _embedding_identity() -> Tuple[Optional[str], Optional[str]]:
    """Nom et version du modele de vectorisation reellement utilise."""
    try:
        model = await model_manager.get_embedding_model()
    except Exception as exc:
        logger.warning(f"[consumers] modele d'embedding indisponible: {exc}")
        return None, None
    if model is None:
        return None, None
    name = getattr(model, "model_name", None) or getattr(model, "name", None)
    provider = getattr(model, "provider", None)
    version = f"{provider}:{name}" if provider and name else (name or None)
    return name, version


@command(
    "consumer_ingest_source",
    app="open_notebook",
    retry={
        "max_attempts": 5,
        "wait_strategy": "exponential_jitter",
        "wait_min": 1,
        "wait_max": 60,
        "stop_on": [ValueError],
        "retry_log_level": "debug",
    },
)
async def consumer_ingest_source_command(
    input_data: ConsumerIngestInput,
) -> ConsumerIngestOutput:
    """Extrait, decoupe et vectorise une version de source pour un consommateur."""
    start = time.time()
    version_id = input_data.version_id
    item_id = input_data.item_id

    async with _version_lock(version_id):
        try:
            await _set_status(version_id, item_id, "extracting")

            content_state: Dict[str, Any] = {}
            if input_data.source_type == "upload" and input_data.payload_ref:
                content_state["file_path"] = input_data.payload_ref
            elif input_data.source_type == "url" and input_data.url:
                content_state["url"] = input_data.url
            elif input_data.source_type == "text" and input_data.text:
                content_state["content"] = input_data.text
            else:
                raise ValueError("Aucun contenu exploitable n'a ete fourni.")

            async with _EXTRACTION_LIMIT:
                extracted = await content_process({"content_state": content_state})
            extraction = extracted["extraction"]

            full_text = (extraction.content or "").strip()
            if not full_text:
                raise ValueError("Aucun texte n'a pu etre extrait de ce document.")

            title = input_data.title or extraction.title or "Document"

            source = Source(
                title=title,
                full_text=full_text,
                asset=Asset(
                    file_path=input_data.payload_ref, url=input_data.url
                ),
            )
            await source.save()

            await _set_status(version_id, item_id, "chunking")

            content_type = detect_content_type(full_text, input_data.payload_ref)
            chunks = chunk_text(full_text, content_type=content_type)
            if not chunks:
                raise ValueError("Le decoupage n'a produit aucun bloc exploitable.")
            records = build_chunk_records(full_text, chunks)

            await _set_status(version_id, item_id, "embedding")

            async with _EMBEDDING_LIMIT:
                embeddings = await generate_embeddings(
                    [r.content for r in records],
                    command_id=str(input_data.execution_context.command_id)
                    if input_data.execution_context
                    else None,
                )

            if len(embeddings) != len(records):
                raise RuntimeError(
                    "Le nombre de vecteurs ne correspond pas au nombre de blocs."
                )

            model_name, model_version = await _embedding_identity()
            dimensions = len(embeddings[0]) if embeddings else 0

            rows: List[Dict[str, Any]] = [
                {
                    "source": ensure_record_id(str(source.id)),
                    "source_version": ensure_record_id(version_id),
                    "order": record.order,
                    "content": record.content,
                    "content_hash": record.content_hash,
                    "page_number": record.page_number,
                    "section_title": record.section_title,
                    "start_offset": record.start_offset,
                    "end_offset": record.end_offset,
                    "embedding": vector,
                    "embedding_model_version": model_version,
                    "embedding_dimensions": dimensions,
                }
                for record, vector in zip(records, embeddings)
            ]
            await repo_insert("source_embedding", rows)

            pages = max(
                [r.page_number for r in records if r.page_number] or [0]
            ) or None

            await _set_status(
                version_id,
                item_id,
                "ready",
                extra={
                    "source": ensure_record_id(str(source.id)),
                    "chunks": len(rows),
                    "pages": pages,
                    "embedding_model": model_name,
                    "embedding_model_version": model_version,
                    "embedding_dimensions": dimensions,
                    "parser_name": "content-core",
                    "parser_version": _content_core_version(),
                    "title": title,
                },
            )
            await repo_query(
                "UPDATE $item SET source_version = $version, updated = time::now()",
                {
                    "item": ensure_record_id(item_id),
                    "version": ensure_record_id(version_id),
                },
            )

            logger.info(
                f"[consumers] version prete version={version_id} blocs={len(rows)} "
                f"modele={model_version}"
            )
            return ConsumerIngestOutput(
                success=True,
                version_id=version_id,
                chunks_created=len(rows),
                processing_time=time.time() - start,
            )

        except ValueError as exc:
            message = _sanitise(str(exc))
            await _set_status(
                version_id,
                item_id,
                "failed",
                error_code="EXTRACTION_FAILED",
                error_message=message,
            )
            logger.error(
                f"[consumers] echec definitif version={version_id} code=EXTRACTION_FAILED"
            )
            return ConsumerIngestOutput(
                success=False,
                version_id=version_id,
                processing_time=time.time() - start,
                error_code="EXTRACTION_FAILED",
                error_message=message,
            )
        except Exception as exc:
            message = _sanitise(str(exc))
            await _set_status(
                version_id,
                item_id,
                "failed",
                error_code="EMBEDDING_FAILED",
                error_message=message,
            )
            logger.error(
                f"[consumers] echec version={version_id} code=EMBEDDING_FAILED"
            )
            raise


def _content_core_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("content-core")
    except Exception:
        return None
