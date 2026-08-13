"""Resolution et garantie du perimetre documentaire.

Toute lecture de la facade passe par ce module. La regle est unique: un
consommateur ne voit que les sources rattachees a un corpus de SON
organisation, et cette organisation est derivee du jeton, jamais du corps de
la requete.

Absence de fuite d'existence: une source hors perimetre renvoie toujours la
meme erreur, qu'elle existe ou non dans Diwan. Un client ne peut donc pas se
servir des codes de retour pour deviner le contenu d'une autre organisation.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from open_notebook.consumers.auth import ConsumerIdentity
from open_notebook.consumers.errors import (
    CORPUS_NOT_FOUND,
    SOURCE_NOT_AUTHORIZED,
    ConsumerAPIError,
)
from open_notebook.database.repository import ensure_record_id, repo_query


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


async def ensure_consumer(consumer_id: str) -> str:
    """Retourne l'identifiant interne du consommateur, en le creant au besoin."""
    rows = await repo_query(
        "SELECT * FROM consumer WHERE name = $name LIMIT 1", {"name": consumer_id}
    )
    if rows:
        return _as_str(rows[0]["id"])
    created = await repo_query(
        "CREATE consumer SET name = $name, active = true", {"name": consumer_id}
    )
    return _as_str(created[0]["id"])


async def resolve_organization(identity: ConsumerIdentity) -> str:
    """Traduit l'identite authentifiee en organisation interne.

    C'est le mapping serveur exige par le contrat: l'organisation n'est jamais
    lue depuis la requete du client.
    """
    consumer_ref = await ensure_consumer(identity.consumer_id)
    rows = await repo_query(
        """
        SELECT * FROM external_organization
        WHERE consumer = $consumer AND external_id = $external_id
        LIMIT 1
        """,
        {
            "consumer": ensure_record_id(consumer_ref),
            "external_id": identity.organization_external_id,
        },
    )
    if rows:
        if rows[0].get("revoked"):
            raise ConsumerAPIError(SOURCE_NOT_AUTHORIZED)
        return _as_str(rows[0]["id"])

    created = await repo_query(
        """
        CREATE external_organization SET
            consumer = $consumer,
            external_id = $external_id,
            revoked = false
        """,
        {
            "consumer": ensure_record_id(consumer_ref),
            "external_id": identity.organization_external_id,
        },
    )
    organization_id = _as_str(created[0]["id"])
    logger.info(
        f"[consumers] organisation enregistree consumer={identity.consumer_id} "
        f"organization={organization_id}"
    )
    return organization_id


async def create_corpus(organization_id: str, name: str) -> Dict[str, Any]:
    created = await repo_query(
        """
        CREATE corpus SET
            organization = $organization,
            name = $name,
            revoked = false
        """,
        {"organization": ensure_record_id(organization_id), "name": name},
    )
    return created[0]


async def get_corpus_or_fail(organization_id: str, corpus_id: str) -> Dict[str, Any]:
    """Charge un corpus en verifiant son rattachement a l'organisation.

    Un corpus d'une autre organisation est traite exactement comme un corpus
    inexistant.
    """
    rows = await repo_query(
        """
        SELECT * FROM corpus
        WHERE id = $corpus AND organization = $organization AND revoked = false
        LIMIT 1
        """,
        {
            "corpus": ensure_record_id(corpus_id),
            "organization": ensure_record_id(organization_id),
        },
    )
    if not rows:
        raise ConsumerAPIError(CORPUS_NOT_FOUND, details={"corpusId": corpus_id})
    return rows[0]


async def authorized_versions(
    organization_id: str,
    source_ids: Sequence[str],
    *,
    corpus_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Traduit des identifiants de source en versions autorisees.

    Le filtre d'autorisation est applique DANS la requete: seules les versions
    rattachees a un corpus non revoque de cette organisation remontent. Toute
    source demandee qui ne ressort pas de cette requete est refusee, sans que
    la reponse permette de distinguer "inexistante" de "appartient a autrui".
    """
    if not source_ids:
        return []

    params: Dict[str, Any] = {
        "organization": ensure_record_id(organization_id),
        "sources": [ensure_record_id(s) for s in source_ids],
    }
    corpus_clause = ""
    if corpus_id:
        corpus_clause = "AND corpus = $corpus"
        params["corpus"] = ensure_record_id(corpus_id)

    rows = await repo_query(
        f"""
        SELECT
            source_version.id AS version_id,
            source_version.source AS source_id,
            source_version.version AS version,
            source_version.title AS title,
            source_version.original_name AS original_name,
            source_version.media_type AS media_type,
            source_version.checksum_sha256 AS checksum_sha256,
            source_version.status AS status,
            source_version.pages AS pages,
            source_version.chunks AS chunks,
            source_version.parser_name AS parser_name,
            source_version.parser_version AS parser_version,
            source_version.embedding_model AS embedding_model,
            source_version.embedding_model_version AS embedding_model_version,
            source_version.embedding_dimensions AS embedding_dimensions,
            source_version.created AS created,
            corpus.id AS corpus_id
        FROM corpus_source
        WHERE revoked = false
            {corpus_clause}
            AND corpus IN (
                SELECT VALUE id FROM corpus
                WHERE organization = $organization AND revoked = false
            )
            AND source_version.source IN $sources
        """,
        params,
    )

    found = {_as_str(row["source_id"]) for row in rows}
    missing = [s for s in source_ids if s not in found]
    if missing:
        raise ConsumerAPIError(
            SOURCE_NOT_AUTHORIZED, details={"sourceIds": sorted(set(missing))}
        )
    return rows


async def list_corpus_sources(
    organization_id: str,
    *,
    corpus_id: Optional[str] = None,
    status: Optional[str] = None,
    query: Optional[str] = None,
    media_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    """Liste paginee des sources autorisees pour cette organisation.

    Ne retourne jamais le texte integral: seules les metadonnees de provenance
    circulent sur cette route.
    """
    # Le filtre d'organisation passe par une sous-requete sur les corpus:
    # SurrealDB ne dereference pas un lien (corpus.organization) dans une clause
    # WHERE, il le fait seulement en projection. Ecrite en traversee, la clause
    # ne remontait donc AUCUNE ligne, ce qui donnait une bibliotheque vide.
    params: Dict[str, Any] = {"organization": ensure_record_id(organization_id)}
    clauses = [
        "revoked = false",
        "corpus IN (SELECT VALUE id FROM corpus "
        "WHERE organization = $organization AND revoked = false)",
    ]
    if corpus_id:
        clauses.append("corpus = $corpus")
        params["corpus"] = ensure_record_id(corpus_id)
    if status:
        clauses.append("source_version.status = $status")
        params["status"] = status
    if media_type:
        clauses.append("source_version.media_type = $media_type")
        params["media_type"] = media_type
    if query:
        clauses.append(
            "(string::contains(string::lowercase(source_version.title OR ''), $query) "
            "OR string::contains(string::lowercase(source_version.original_name OR ''), $query))"
        )
        params["query"] = query.lower()

    where = " AND ".join(clauses)

    total_rows = await repo_query(
        f"SELECT VALUE count() FROM corpus_source WHERE {where} GROUP ALL", params
    )
    # Selon la version du moteur, un count() agrege revient soit en scalaire,
    # soit dans un objet {"count": n}. Les deux formes sont acceptees.
    total: Any = total_rows[0] if total_rows else 0
    if isinstance(total, dict):
        total = total.get("count", 0)

    params["limit"] = max(1, min(page_size, 100))
    params["start"] = max(0, (max(1, page) - 1) * params["limit"])

    rows = await repo_query(
        f"""
        SELECT
            source_version.id AS version_id,
            source_version.source AS source_id,
            source_version.title AS title,
            source_version.original_name AS original_name,
            source_version.media_type AS media_type,
            source_version.checksum_sha256 AS checksum_sha256,
            source_version.status AS status,
            source_version.pages AS pages,
            source_version.chunks AS chunks,
            source_version.created AS created,
            corpus.id AS corpus_id
        FROM corpus_source
        WHERE {where}
        ORDER BY source_version.created DESC
        LIMIT $limit START $start
        """,
        params,
    )
    return rows, int(total or 0)
