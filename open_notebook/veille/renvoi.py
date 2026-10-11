"""Renvoi à Saqr d'un épisode de veille choisi à la main (bouton « Renvoyer à Saqr »).

Cas visé: un épisode « Veille Saqr AAAA-MM-JJ » produit hors du circuit de l'API (studio, relance manuelle)
n'annonce jamais « pret » à Saqr, qui affiche alors « echec » alors que l'audio existe (08/10 et 11/10).

Règles (contrat de Saqr, confirmé par sa session le 11/10):
- la révision envoyée est celle de la DEMANDE de Saqr, lue dans la ligne de suivi, jamais déduite du texte;
- un épisode créé avant cette demande correspond à une autre révision: il est refusé;
- un nouveau « pret » remplace l'épisode précédent chez Saqr, un « echec » tardif n'écrase jamais un « pret »;
- l'audio doit être servi par un domaine ai-mpower.com (contrôlé par Saqr, son refus est rapporté tel quel).
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from loguru import logger

from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.podcasts.audio_paths import resolve_contained_audio_path
from open_notebook.veille import runs
from open_notebook.veille.audio_check import AudioInvalid, probe_mp3
from open_notebook.veille.pipeline import audio_url_for, page_url_for, sanitize
from open_notebook.veille.saqr_client import (
    SaqrRejected,
    SaqrUnavailable,
    callback_payload,
    post_callback,
)

NAME_PATTERN = re.compile(r"^Veille Saqr (\d{4}-\d{2}-\d{2})$")
# Même marge que pipeline.episodes_of_run: écart entre la réception de la demande et la création de l'épisode.
REQUEST_MARGIN_SECONDS = 120
# Champs de la ligne de suivi remis tels quels si Saqr refuse le renvoi.
_RESTORED = ("statut", "etape", "erreur", "episode", "audio_url", "duree_s", "rappel_statut", "rappel_tentatives")


class RenvoiRefused(Exception):
    """Le renvoi n'est pas possible; `status` est le code HTTP à rendre."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def ref_of_name(name: Optional[str]) -> Optional[str]:
    """« Veille Saqr 2026-10-11 » -> « veille-2026-10-11 »; None pour tout autre nom."""
    match = NAME_PATTERN.match(name or "")
    return f"veille-{match.group(1)}" if match else None


async def _episode(episode_id: str, since: float) -> Dict[str, Any]:
    """L'épisode et son âge par rapport à la demande, en une requête (le filtre est celui d'episodes_of_run)."""
    try:
        record = ensure_record_id(episode_id)
    except Exception as exc:  # noqa: BLE001 - un identifiant mal formé est une erreur de l'appelant, pas du serveur
        raise RenvoiRefused(404, "Épisode introuvable.") from exc
    rows = await repo_query(
        "SELECT *, created >= type::datetime($since) AS apres_demande FROM $id",
        {"id": record, "since": datetime.fromtimestamp(since, tz=timezone.utc).isoformat()},
    )
    if not rows:
        raise RenvoiRefused(404, "Épisode introuvable.")
    return rows[0]


async def resend(episode_id: str) -> Dict[str, Any]:
    """Rattache l'épisode à sa veille puis annonce « pret » à Saqr (un seul essai: la reprise rejoue le reste)."""
    # Le nom donne la veille; on lit d'abord l'épisode sans filtre d'âge pour connaître son nom.
    probe = await _episode(episode_id, 0.0)
    ref = ref_of_name(probe.get("name"))
    if ref is None:
        raise RenvoiRefused(422, "Seul un épisode « Veille Saqr AAAA-MM-JJ » peut être renvoyé à Saqr.")
    run = await runs.get_run(ref)
    if run is None:
        raise RenvoiRefused(404, f"Saqr n'a jamais demandé la veille {ref} : rien à lui renvoyer.")
    if run.get("statut") in runs.ACTIVE:
        raise RenvoiRefused(409, "Une production de cette veille est en cours : attendez sa fin.")

    started = float(run.get("echeance") or 0) - runs.deadline_seconds() - REQUEST_MARGIN_SECONDS
    episode = await _episode(episode_id, started)
    if not episode.get("apres_demande"):
        raise RenvoiRefused(
            409, "Cet épisode date d'avant la dernière demande de Saqr (autre révision) : il ne correspond plus."
        )
    if not episode.get("audio_file"):
        raise RenvoiRefused(409, "Cet épisode n'a pas encore d'audio.")
    path = resolve_contained_audio_path(episode["audio_file"])
    if path is None:
        raise RenvoiRefused(409, "Le fichier audio de cet épisode est hors du dossier autorisé.")
    try:
        info = await probe_mp3(path)
    except AudioInvalid as exc:
        raise RenvoiRefused(409, f"Fichier audio refusé : {exc}") from exc

    previous = {key: run.get(key) for key in _RESTORED}
    episode_key = str(episode["id"])
    await runs.update_run(
        str(run["id"]), statut="pret", etape=None, erreur=None, episode=episode_key,
        audio_url=audio_url_for(episode_key), duree_s=info["duration_s"],
        rappel_statut="a_envoyer", rappel_tentatives=0,
    )
    current = await runs.get_run(ref)
    if current is None:  # la ligne vient d'être écrite: son absence est une panne de base, pas un cas métier
        raise RenvoiRefused(503, "La ligne de suivi est illisible : réessayez.")
    payload = callback_payload(current, page_url=page_url_for(current))
    try:
        await post_callback(payload)
    except SaqrRejected as exc:
        # Saqr n'a rien retenu: la ligne reprend son état d'avant, sinon elle annoncerait un épisode inconnu de Saqr.
        await runs.update_run(str(run["id"]), **previous)
        logger.error(f"[veille] renvoi de {episode_key} refusé par Saqr : {sanitize(str(exc))}")
        raise RenvoiRefused(502, sanitize(str(exc))) from exc
    except SaqrUnavailable as exc:
        # L'épisode reste rattaché, le rappel reste « a_envoyer »: la boucle de reprise le rejoue toutes les minutes.
        logger.warning(f"[veille] renvoi de {episode_key} : Saqr injoignable, rejeu automatique")
        raise RenvoiRefused(503, "Saqr est injoignable : le renvoi sera rejoué automatiquement dans la minute.") from exc
    await runs.update_run(str(run["id"]), rappel_statut="envoye", rappel_tentatives=1)
    logger.info(f"[veille] {ref} : épisode {episode_key} renvoyé à Saqr")
    return {
        "ref": ref,
        "episode": episode_key,
        "revision": current["revision"],
        "dureeS": payload["duree_s"],
        "audioUrl": payload["audio_url"],
        "rappel": "envoye",
    }
