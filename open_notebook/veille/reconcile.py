"""Reprise des podcasts de veille: jamais le silence, même si un service redémarre en route.

Le worker (qui produit) et l'API (qui reçoit) sont deux processus. La ligne `veille_run` est donc
la seule mémoire commune. Une boucle de fond de l'API, toutes les minutes:
1. rejoue les rappels vers Saqr qui n'ont pas abouti;
2. conclut en échec une production dont l'échéance est passée (Saqr publie alors sans podcast,
   mais il le SAIT);
3. relance la commande d'une production dont le battement s'est arrêté (worker redémarré),
   au plus MAX_RESUBMITS fois.
"""

import asyncio
import time
from typing import Any, Dict

from loguru import logger

from open_notebook.veille import pipeline, runs

INTERVAL_SECONDS = 60
STALE_SECONDS = 120
MAX_RESUBMITS = 3


def _started_at(run: Dict[str, Any]) -> float:
    """Réception approximative de la demande, à partir de l'échéance."""
    return float(run.get("echeance") or 0) - runs.deadline_seconds()


async def _expire(run: Dict[str, Any], message: str) -> None:
    await pipeline.fail(str(run["id"]), message)
    final = await runs.get_run(run["ref"])
    if final:
        await pipeline.deliver_callback(final, attempts=1)


async def reconcile_once() -> None:
    for run in await runs.list_pending_callbacks():
        await pipeline.deliver_callback(run, attempts=1)

    now = time.time()
    for run in await runs.list_active():
        if pipeline.time_left(run) <= 0:
            await _expire(run, "Délai de production dépassé.")
            continue
        alive_since = max(float(run.get("battement") or 0), _started_at(run))
        if now - alive_since <= STALE_SECONDS:
            continue
        resubmits = int(run.get("reprises") or 0)
        if resubmits >= MAX_RESUBMITS:
            await _expire(run, "La production a été interrompue à plusieurs reprises.")
            continue
        logger.warning(f"[veille] {run['ref']} sans battement, reprise {resubmits + 1}/{MAX_RESUBMITS}")
        await runs.update_run(str(run["id"]), reprises=resubmits + 1, battement=now)
        await runs.update_run(str(run["id"]), job=pipeline.submit_orchestrator(run["ref"]))


async def reconcile_forever() -> None:
    """Boucle de fond lancée au démarrage de l'API; une erreur n'arrête jamais la boucle."""
    while True:
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - la surveillance doit survivre à une base momentanément absente
            logger.exception("[veille] la reprise a échoué, nouvel essai à la prochaine minute")
        await asyncio.sleep(INTERVAL_SECONDS)
