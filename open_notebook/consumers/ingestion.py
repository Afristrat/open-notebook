"""Ingestion versionnee pour la facade consommateur.

Difference de fond avec le chemin historique de Diwan: chaque import produit
une VERSION IMMUABLE de la source. Les blocs sont rattaches a cette version et
ne sont jamais remplaces par un import ulterieur. Une classroom qui cite un
passage continue donc de citer exactement le meme texte, meme si le document
est reimporte plus tard dans une version corrigee.

Le chemin historique (embed_source_command) supprime au contraire les blocs
existants a chaque vectorisation. Il n'est pas modifie ici: les usages actuels
de Diwan continuent de fonctionner a l'identique.
"""

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from open_notebook.config import DATA_FOLDER
from open_notebook.consumers.errors import (
    FILE_TOO_LARGE,
    INVALID_SOURCE_TYPE,
    ConsumerAPIError,
)
from open_notebook.database.repository import ensure_record_id, repo_query

CONSUMER_STORAGE = Path(DATA_FOLDER) / "consumers" / "originals"

# Signatures binaires des formats acceptes. La validation porte sur le CONTENU:
# une extension .pdf sur un fichier qui n'en est pas un est refusee.
_SIGNATURES: List[Tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"{\\rtf", "application/rtf"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),
]

# Conteneurs OOXML: un .docx/.pptx/.xlsx est un zip dont le contenu tranche.
_OOXML_MARKERS: List[Tuple[bytes, str]] = [
    (b"word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (
        b"ppt/",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    (b"xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
]

_TEXTUAL_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
}

ALLOWED_MEDIA_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/png",
    "image/jpeg",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "text/html",
    "application/rtf",
}


def max_upload_bytes() -> int:
    raw = os.getenv("DIWAN_CONSUMER_MAX_UPLOAD_MB")
    try:
        megabytes = int(raw) if raw else 50
    except ValueError:
        megabytes = 50
    return max(1, megabytes) * 1024 * 1024


def max_files_per_request() -> int:
    raw = os.getenv("DIWAN_CONSUMER_MAX_FILES")
    try:
        value = int(raw) if raw else 20
    except ValueError:
        value = 20
    return max(1, value)


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _looks_textual(payload: bytes) -> bool:
    sample = payload[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def detect_media_type(filename: str, payload: bytes) -> str:
    """Determine le type reel a partir du contenu, puis le confronte au nom.

    Le nom de fichier ne sert qu'a departager les formats textuels, qui n'ont
    pas de signature binaire. Un fichier dont le contenu contredit l'extension
    annoncee est refuse.
    """
    if not payload:
        raise ConsumerAPIError(
            INVALID_SOURCE_TYPE, message="Le fichier fourni est vide."
        )

    detected: Optional[str] = None
    for signature, media_type in _SIGNATURES:
        if payload.startswith(signature):
            detected = media_type
            break

    if detected == "application/zip":
        head = payload[:8192]
        detected = None
        for marker, media_type in _OOXML_MARKERS:
            if marker in head:
                detected = media_type
                break
        if detected is None:
            raise ConsumerAPIError(
                INVALID_SOURCE_TYPE,
                message="Ce conteneur compresse n'est pas un document bureautique reconnu.",
            )

    extension = Path(filename or "").suffix.lower()

    if detected is None:
        if _looks_textual(payload):
            detected = _TEXTUAL_EXTENSIONS.get(extension, "text/plain")
        else:
            raise ConsumerAPIError(
                INVALID_SOURCE_TYPE,
                message="Le contenu de ce fichier ne correspond a aucun format accepte.",
            )
    else:
        # Le contenu fait foi: on refuse une extension qui annonce autre chose.
        announced, _ = mimetypes.guess_type(filename or "")
        if announced and announced != detected:
            binary_announced = announced not in _TEXTUAL_EXTENSIONS.values()
            if binary_announced:
                raise ConsumerAPIError(
                    INVALID_SOURCE_TYPE,
                    message=(
                        "L'extension de ce fichier ne correspond pas a son contenu reel."
                    ),
                    details={"declared": announced, "detected": detected},
                )

    if detected not in ALLOWED_MEDIA_TYPES:
        raise ConsumerAPIError(
            INVALID_SOURCE_TYPE, details={"detected": detected}
        )
    return detected


def validate_upload(filename: str, payload: bytes) -> str:
    if len(payload) > max_upload_bytes():
        raise ConsumerAPIError(
            FILE_TOO_LARGE,
            details={"maxBytes": max_upload_bytes(), "receivedBytes": len(payload)},
        )
    return detect_media_type(filename, payload)


def store_original(payload: bytes, checksum: str, media_type: str) -> str:
    """Conserve le fichier d'origine dans le volume persistant de Diwan.

    Le nom de stockage est l'empreinte du contenu: deux imports du meme fichier
    partagent le meme objet, et un fichier ne peut pas en ecraser un autre par
    collision de nom.
    """
    CONSUMER_STORAGE.mkdir(parents=True, exist_ok=True)
    extension = mimetypes.guess_extension(media_type) or ".bin"
    target = CONSUMER_STORAGE / f"{checksum}{extension}"
    if not target.exists():
        target.write_bytes(payload)
    return str(target)


@dataclass
class ChunkRecord:
    order: int
    content: str
    section_title: Optional[str]
    page_number: Optional[int]
    start_offset: int
    end_offset: int

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_PAGE_MARKER = re.compile(r"(?:^|\n)\s*(?:page|Page)\s+(\d{1,4})\s*(?:\n|$)")


def _section_index(text: str) -> List[Tuple[int, str]]:
    """Position de depart de chaque titre de section markdown."""
    return [(m.start(), m.group(1).strip()) for m in _HEADING.finditer(text)]


def _page_index(text: str) -> List[Tuple[int, int]]:
    """Position de depart des marqueurs de page, lorsqu'il y en a."""
    return [(m.start(), int(m.group(1))) for m in _PAGE_MARKER.finditer(text)]


def _lookup(index: List[Tuple[int, Any]], offset: int) -> Optional[Any]:
    found = None
    for position, value in index:
        if position <= offset:
            found = value
        else:
            break
    return found


def build_chunk_records(full_text: str, chunks: List[str]) -> List[ChunkRecord]:
    """Rattache chaque bloc a sa position, sa section et sa page si connue.

    Les offsets sont retrouves par recherche progressive dans le texte source,
    ce qui garde la provenance exacte meme quand le decoupeur normalise les
    espaces en bordure de bloc.
    """
    sections = _section_index(full_text)
    pages = _page_index(full_text)
    records: List[ChunkRecord] = []
    cursor = 0
    for order, chunk in enumerate(chunks):
        probe = chunk.strip()[:120]
        start = full_text.find(probe, cursor) if probe else -1
        if start == -1:
            start = cursor
        end = start + len(chunk)
        cursor = max(cursor, start + max(1, len(chunk) // 2))
        records.append(
            ChunkRecord(
                order=order,
                content=chunk,
                section_title=_lookup(sections, start),
                page_number=_lookup(pages, start),
                start_offset=start,
                end_offset=end,
            )
        )
    return records


async def find_existing_version(
    checksum: str, organization_id: str
) -> Optional[Dict[str, Any]]:
    """Cherche une version deja ingeree du meme contenu pour cette organisation.

    Permet de rattacher un document identique a un nouveau corpus sans le
    re-extraire ni le re-vectoriser, tout en restant cloisonne: la recherche ne
    traverse jamais la frontiere de l'organisation.
    """
    rows = await repo_query(
        """
        SELECT source_version.* FROM corpus_source
        WHERE revoked = false
            AND corpus IN (
                SELECT VALUE id FROM corpus
                WHERE organization = $organization AND revoked = false
            )
            AND source_version.checksum_sha256 = $checksum
            AND source_version.status = 'ready'
        LIMIT 1
        """,
        {
            "organization": ensure_record_id(organization_id),
            "checksum": checksum,
        },
    )
    if not rows:
        return None
    row = rows[0]
    return row.get("source_version") if isinstance(row.get("source_version"), dict) else row


async def next_version_number(checksum_source_id: Optional[str]) -> int:
    if not checksum_source_id:
        return 1
    rows = await repo_query(
        "SELECT VALUE math::max(version) FROM source_version WHERE source = $source GROUP ALL",
        {"source": ensure_record_id(checksum_source_id)},
    )
    current = rows[0] if rows else None
    return int(current or 0) + 1


async def attach_to_corpus(corpus_id: str, version_id: str) -> None:
    """Rattache une version a un corpus, sans dupliquer un rattachement existant."""
    await repo_query(
        """
        LET $existing = (SELECT id FROM corpus_source
            WHERE corpus = $corpus AND source_version = $version LIMIT 1);
        IF array::len($existing) = 0 {
            CREATE corpus_source SET
                corpus = $corpus,
                source_version = $version,
                rights = ['read'],
                revoked = false;
        } ELSE {
            UPDATE $existing[0].id SET revoked = false, updated = time::now();
        };
        """,
        {
            "corpus": ensure_record_id(corpus_id),
            "version": ensure_record_id(version_id),
        },
    )
    logger.info(
        f"[consumers] version rattachee corpus={corpus_id} version={version_id}"
    )
