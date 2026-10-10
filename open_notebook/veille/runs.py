"""Suivi durable des podcasts de veille (table veille_run, migration 29).

Une ligne par veille (ref unique). La ligne est la mémoire du service: un redémarrage au
milieu d'une production ne perd ni la demande ni l'annonce due à Saqr (voir reconcile.py).
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from open_notebook.database.repository import ensure_record_id, repo_create, repo_query

ACTIVE = ("accepte", "en_cours")
TERMINAL = ("pret", "echec")
DEFAULT_DEADLINE_MINUTES = 70.0
# SurrealDB refuse une transaction concurrente sur la même ligne et indique qu'elle peut être rejouée
CONFLICT_MARK = "read or write conflict"
UPDATE_ATTEMPTS = 5
UPDATE_BACKOFF_SECONDS = 0.1

# Seuls ces champs peuvent être écrits par update_run: leurs noms entrent dans la requête.
_WRITABLE = {
    "statut", "etape", "tentatives", "job", "episode", "audio_url", "duree_s", "erreur",
    "rapport", "echeance", "rappel_statut", "rappel_tentatives", "battement", "reprises",
}


def deadline_seconds() -> float:
    """Délai accordé à une production, à partir de la réception de la demande."""
    try:
        minutes = float(os.getenv("SAQR_VEILLE_DEADLINE_MINUTES", ""))
    except ValueError:
        minutes = DEFAULT_DEADLINE_MINUTES
    return max(minutes, 1.0) * 60


async def get_run(ref: str) -> Optional[Dict[str, Any]]:
    rows = await repo_query("SELECT * FROM veille_run WHERE ref = $ref LIMIT 1", {"ref": ref})
    return rows[0] if rows else None


async def create_run(
    ref: str,
    revision: str,
    titre: Optional[str],
    date_publication: Optional[str],
    page_url: Optional[str],
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "ref": ref, "revision": revision, "statut": "accepte", "tentatives": 0,
        "rappel_statut": "a_envoyer", "rappel_tentatives": 0,
        "echeance": time.time() + deadline_seconds(),
    }
    # Un champ optionnel absent reste NONE: un None envoyé serait lu comme NULL et refusé.
    for key, value in (("titre", titre), ("date_publication", date_publication), ("page_url", page_url)):
        if value:
            data[key] = value
    await repo_create("veille_run", data)
    run = await get_run(ref)
    if run is None:
        raise RuntimeError("La demande de veille n'a pas pu être enregistrée.")
    return run


async def relaunch_run(
    run_id: str,
    revision: str,
    titre: Optional[str],
    date_publication: Optional[str],
    page_url: Optional[str],
) -> None:
    """Repart de zéro sur une veille dont l'essai précédent a échoué."""
    await repo_query(
        "UPDATE $id SET revision = $revision, titre = $titre ?? NONE, "
        "date_publication = $date ?? NONE, page_url = $page ?? NONE, statut = 'accepte', "
        "etape = NONE, job = NONE, episode = NONE, audio_url = NONE, duree_s = NONE, "
        "erreur = NONE, rapport = NONE, tentatives = 0, rappel_statut = 'a_envoyer', "
        "rappel_tentatives = 0, reprises = 0, battement = NONE, echeance = $echeance, "
        "updated = time::now()",
        {
            "id": ensure_record_id(run_id), "revision": revision, "titre": titre,
            "date": date_publication, "page": page_url,
            "echeance": time.time() + deadline_seconds(),
        },
    )


async def update_run(run_id: str, **fields: Any) -> None:
    """Met à jour des champs; une valeur None efface le champ (NONE)."""
    unknown = set(fields) - _WRITABLE
    if unknown:
        raise ValueError(f"Champs de suivi inconnus : {sorted(unknown)}")
    assignments, params = [], {"id": ensure_record_id(run_id)}
    for key, value in fields.items():
        if value is None:
            assignments.append(f"{key} = NONE")
        else:
            assignments.append(f"{key} = ${key}")
            params[key] = value
    if not assignments:
        return
    query = f"UPDATE $id SET {', '.join(assignments)}, updated = time::now()"
    for attempt in range(UPDATE_ATTEMPTS):
        try:
            await repo_query(query, params)
            return
        except Exception as exc:  # noqa: BLE001 - seul le conflit d'écriture est rejoué, le reste remonte
            if CONFLICT_MARK not in str(exc) or attempt == UPDATE_ATTEMPTS - 1:
                raise
            # le battement (_heartbeat) et produce écrivent la même ligne au même instant au démarrage:
            # SurrealDB demande de rejouer la transaction (veille du 09/10, requête de 07:55:01)
            await asyncio.sleep(UPDATE_BACKOFF_SECONDS * (attempt + 1))


async def list_active() -> List[Dict[str, Any]]:
    return await repo_query("SELECT * FROM veille_run WHERE statut IN ['accepte', 'en_cours']")


async def list_recent_failed() -> List[Dict[str, Any]]:
    """Échecs des dernières 36 heures sans épisode lié: un épisode terminé après coup peut encore les rattraper."""
    return await repo_query(
        "SELECT * FROM veille_run WHERE statut = 'echec' AND episode = NONE "
        "AND updated > time::now() - 36h"
    )


async def list_pending_callbacks() -> List[Dict[str, Any]]:
    return await repo_query(
        "SELECT * FROM veille_run WHERE statut IN ['pret', 'echec'] AND rappel_statut = 'a_envoyer'"
    )


def public_view(run: Dict[str, Any]) -> Dict[str, Any]:
    """Ce que Saqr peut lire d'une ligne de suivi."""
    return {
        "ref": run.get("ref"),
        "revision": run.get("revision"),
        "statut": run.get("statut"),
        "etape": run.get("etape"),
        "audioUrl": run.get("audio_url"),
        "dureeS": run.get("duree_s"),
        "erreur": run.get("erreur"),
        "rappel": run.get("rappel_statut"),
    }
