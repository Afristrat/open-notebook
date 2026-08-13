"""Recherche documentaire cloisonnee.

Deux garanties tiennent tout ce module:

1. Le filtre de perimetre est applique DANS la requete SurrealDB, jamais apres
   coup en Python. Un bloc hors liste blanche ne quitte donc jamais la base.
2. Une absence de preuve reste une absence de preuve. Aucun repli vers une
   recherche globale, aucun elargissement silencieux du perimetre, aucune
   completion par une recherche Web.

La recherche est hybride: la voie vectorielle capte la proximite semantique,
la voie plein texte rattrape les termes exacts (sigles, references) que les
vecteurs diluent. Les deux voies sont notees sur la meme echelle, la
similarite cosinus, pour qu'un seuil unique reste interpretable.
"""

import hashlib
import re
from typing import Any, Dict, List, Sequence

from loguru import logger

from open_notebook.consumers.errors import (
    EMBEDDING_DIMENSION_MISMATCH,
    EMBEDDING_MODEL_UNAVAILABLE,
    ConsumerAPIError,
)
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.utils.embedding import generate_embedding

MAX_LIMIT = 50


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _normalise(text: str) -> str:
    """Forme normalisee servant a reperer les blocs quasiment identiques."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _fingerprint(text: str) -> str:
    return hashlib.sha256(_normalise(text).encode("utf-8")).hexdigest()


async def embed_query(query: str) -> List[float]:
    """Vectorise la requete, en signalant proprement un modele indisponible."""
    try:
        vector = await generate_embedding(query)
    except Exception as exc:
        logger.error(f"[consumers] vectorisation de la requete impossible: {exc}")
        raise ConsumerAPIError(EMBEDDING_MODEL_UNAVAILABLE)
    if not vector:
        raise ConsumerAPIError(EMBEDDING_MODEL_UNAVAILABLE)
    return vector


async def assert_dimensions_match(
    version_ids: Sequence[str], query_dimensions: int
) -> None:
    """Refuse la recherche si le corpus n'est pas indexe dans la meme dimension.

    La fonction historique fn::vector_search ecarte silencieusement les blocs de
    dimension differente, ce qui produit un resultat vide impossible a
    distinguer d'une absence reelle de preuve. Le contrat exige l'inverse: un
    echec explicite et actionnable.
    """
    if not version_ids:
        return
    rows = await repo_query(
        """
        SELECT VALUE array::distinct(array::group(dimension)) FROM (
            SELECT array::len(embedding) AS dimension
            FROM source_embedding
            WHERE source_version IN $versions AND embedding != NONE
        ) GROUP ALL
        """,
        {"versions": [ensure_record_id(v) for v in version_ids]},
    )
    dimensions = [d for d in (rows[0] if rows else []) if d]
    mismatched = [d for d in dimensions if d != query_dimensions]
    if mismatched:
        raise ConsumerAPIError(
            EMBEDDING_DIMENSION_MISMATCH,
            details={
                "queryDimensions": query_dimensions,
                "corpusDimensions": sorted(set(mismatched)),
            },
        )


async def _cosine_for(
    chunk_ids: Sequence[str], query_vector: List[float]
) -> Dict[str, float]:
    """Score cosinus des blocs remontes par la voie plein texte.

    Les deux voies sont ainsi notees sur la meme echelle et un seuil unique
    garde un sens.
    """
    if not chunk_ids:
        return {}
    rows = await repo_query(
        """
        SELECT id, vector::similarity::cosine(embedding, $query) AS similarity
        FROM source_embedding
        WHERE id IN $ids AND embedding != NONE
            AND array::len(embedding) = array::len($query)
        """,
        {
            "ids": [ensure_record_id(c) for c in chunk_ids],
            "query": query_vector,
        },
    )
    return {_as_str(r["id"]): float(r.get("similarity") or 0.0) for r in rows}


async def scoped_search(
    version_ids: Sequence[str],
    query: str,
    *,
    limit: int = 12,
    minimum_score: float = 0.35,
    search_mode: str = "hybrid",
) -> List[Dict[str, Any]]:
    """Recherche limitee a une liste blanche de versions de source.

    Retourne une liste de preuves ordonnees par score decroissant. Une liste
    vide signifie qu'aucun passage n'atteint le seuil: c'est un resultat, pas
    une erreur, et l'appelant doit le rendre comme insufficient_evidence.
    """
    if not version_ids:
        return []

    limit = max(1, min(int(limit), MAX_LIMIT))
    refs = [ensure_record_id(v) for v in version_ids]

    query_vector = await embed_query(query)
    await assert_dimensions_match(version_ids, len(query_vector))

    collected: Dict[str, Dict[str, Any]] = {}

    if search_mode in ("hybrid", "vector"):
        vector_rows = await repo_query(
            "RETURN fn::scoped_vector_search($query, $versions, $limit, $minimum);",
            {
                "query": query_vector,
                "versions": refs,
                "limit": limit * 3,
                "minimum": float(minimum_score),
            },
        )
        for row in _flatten(vector_rows):
            collected[_as_str(row["id"])] = dict(row)

    if search_mode in ("hybrid", "text"):
        text_rows = await repo_query(
            "RETURN fn::scoped_text_search($text, $versions, $limit);",
            {"text": query, "versions": refs, "limit": limit * 3},
        )
        text_rows = _flatten(text_rows)
        fresh = [r for r in text_rows if _as_str(r["id"]) not in collected]
        cosines = await _cosine_for([_as_str(r["id"]) for r in fresh], query_vector)
        for row in fresh:
            chunk_id = _as_str(row["id"])
            score = cosines.get(chunk_id, 0.0)
            if score >= float(minimum_score):
                entry = dict(row)
                entry["similarity"] = score
                collected[chunk_id] = entry

    ordered = sorted(
        collected.values(), key=lambda r: float(r.get("similarity") or 0.0), reverse=True
    )

    # Deduplication des blocs quasiment identiques. Le recouvrement de decoupage
    # produit des blocs voisins tres proches; deux passages distincts d'une meme
    # source restent en revanche des preuves distinctes et sont conserves.
    seen_fingerprints: set = set()
    evidence: List[Dict[str, Any]] = []
    for row in ordered:
        content = row.get("content") or ""
        fingerprint = row.get("content_hash") or _fingerprint(content)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        evidence.append(row)
        if len(evidence) >= limit:
            break

    return evidence


def _flatten(rows: Any) -> List[Dict[str, Any]]:
    """Aplati le retour d'un RETURN fn::... (liste eventuellement imbriquee)."""
    if not rows:
        return []
    if isinstance(rows, dict):
        return [rows]
    flat: List[Dict[str, Any]] = []
    for item in rows:
        if isinstance(item, list):
            flat.extend(x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def evidence_payload(
    row: Dict[str, Any], version_index: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Met une preuve au format du contrat, provenance comprise."""
    version_id = _as_str(row.get("source_version"))
    version = version_index.get(version_id, {})
    return {
        "chunkId": _as_str(row.get("id")),
        "sourceId": _as_str(row.get("source")),
        "sourceVersion": version_id,
        "sourceTitle": version.get("title") or version.get("original_name"),
        "pageNumber": row.get("page_number"),
        "sectionTitle": row.get("section_title"),
        "content": row.get("content") or "",
        "score": round(float(row.get("similarity") or 0.0), 4),
        "contentHash": row.get("content_hash"),
        "sourceChecksumSha256": version.get("checksum_sha256"),
    }
