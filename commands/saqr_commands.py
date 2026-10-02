"""Commande du podcast de veille demandé par Saqr.

Dans la file durable de Diwan (surreal-commands), comme le reste. Aucune reprise automatique du
job lui-même (max_attempts 1): relancer la commande entière dupliquerait des épisodes. Les relances
utiles (contrôle de contenu, reprise au clip, rappel) vivent dans open_notebook/veille/pipeline.py,
et un job mort est repris par open_notebook/veille/reconcile.py.
"""

from typing import Optional

from loguru import logger
from surreal_commands import CommandInput, CommandOutput, command

from open_notebook.veille.pipeline import run_veille


class SaqrVeilleInput(CommandInput):
    ref: str


class SaqrVeilleOutput(CommandOutput):
    success: bool
    ref: str
    error_message: Optional[str] = None


@command("saqr_veille_podcast", app="open_notebook", retry={"max_attempts": 1})
async def saqr_veille_podcast_command(input_data: SaqrVeilleInput) -> SaqrVeilleOutput:
    """Produit le podcast d'une veille de Saqr puis annonce l'issue à Saqr."""
    try:
        await run_veille(input_data.ref)
    except Exception as exc:  # noqa: BLE001 - run_veille annonce déjà les échecs; ceci est le filet
        logger.exception(f"[veille] la commande a échoué pour {input_data.ref}")
        return SaqrVeilleOutput(success=False, ref=input_data.ref, error_message=type(exc).__name__)
    return SaqrVeilleOutput(success=True, ref=input_data.ref)
