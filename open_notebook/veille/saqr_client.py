"""Appels sortants vers Saqr: lecture d'une veille publiée, rappel « pret » ou « echec ».

Deux jetons DISTINCTS, un par sens (décision commune du 02/10/2026):
- SAQR_DIWAN_SOURCES_TOKEN: Diwan lit chez Saqr (fonction edge veille-sources, lecture seule);
- DIWAN_SAQR_PODCAST_TOKEN: Diwan rappelle Saqr (fonction edge veille-podcast).
Le sens inverse (Saqr vers Diwan) est authentifié par empreinte, voir open_notebook/consumers/auth.py.

Aucun jeton n'est journalisé ni renvoyé dans une erreur.
"""

import os
from typing import Any, Dict, Optional

import httpx

from open_notebook.utils.encryption import get_secret_from_env

DEFAULT_SOURCES_URL = "https://db.saqr.ma/functions/v1/veille-sources"
DEFAULT_CALLBACK_URL = "https://db.saqr.ma/functions/v1/veille-podcast"
TIMEOUT_SECONDS = 30.0


class SaqrUnavailable(RuntimeError):
    """Saqr injoignable ou en erreur: l'appel peut être rejoué."""


class SaqrRejected(ValueError):
    """Saqr refuse définitivement la demande (pièce absente, jeton refusé): inutile de rejouer."""


def _token(name: str) -> str:
    value = (get_secret_from_env(name) or "").strip()
    if not value:
        raise SaqrRejected(f"Le jeton {name} n'est pas configuré côté Diwan.")
    return value


def _reason(response: httpx.Response) -> str:
    """Raison donnée par Saqr (« duree_invalide », « audio_hote_refuse »…), si lisible; jamais un jeton."""
    try:
        body = response.json()
        reason = str(body.get("erreur") or body.get("error") or "") if isinstance(body, dict) else ""
    except ValueError:
        reason = ""
    return f" : {reason[:80]}" if reason else ""


def _check(response: httpx.Response, what: str) -> None:
    # 400 = corps refusé, 404 = jeton faux ou révision non demandée: rejouer ne changerait rien.
    if response.status_code in (400, 401, 403, 404):
        raise SaqrRejected(f"{what} refusé par Saqr (HTTP {response.status_code}{_reason(response)}).")
    if response.status_code >= 400:
        raise SaqrUnavailable(f"{what} en erreur chez Saqr (HTTP {response.status_code}).")


async def fetch_piece(ref: str) -> Dict[str, Any]:
    """Pièce publiée (contenu markdown et sources), 404 chez Saqr si absente ou non publiée."""
    url = os.getenv("SAQR_VEILLE_SOURCES_URL", DEFAULT_SOURCES_URL)
    headers = {"Authorization": f"Bearer {_token('SAQR_DIWAN_SOURCES_TOKEN')}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(url, params={"slug": ref}, headers=headers)
    except httpx.HTTPError as exc:
        raise SaqrUnavailable(f"Saqr injoignable ({type(exc).__name__}).") from exc
    _check(response, "La lecture de la veille")
    piece = response.json()
    if not isinstance(piece, dict) or not piece.get("contenu") or not piece.get("sources"):
        raise SaqrRejected("La pièce publiée par Saqr est incomplète (contenu ou sources absents).")
    return piece


async def post_callback(payload: Dict[str, Any]) -> None:
    """Annonce à Saqr le résultat; lève SaqrUnavailable si le rappel doit être rejoué."""
    url = os.getenv("SAQR_VEILLE_CALLBACK_URL", DEFAULT_CALLBACK_URL)
    headers = {"Authorization": f"Bearer {_token('DIWAN_SAQR_PODCAST_TOKEN')}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise SaqrUnavailable(f"Saqr injoignable ({type(exc).__name__}).") from exc
    _check(response, "Le rappel")


def callback_payload(
    run: Dict[str, Any], *, page_url: Optional[str] = None
) -> Dict[str, Any]:
    """Corps du rappel, à partir de la ligne de suivi."""
    ready = run.get("statut") == "pret"
    return {
        "ref": run["ref"],
        "revision": run["revision"],
        "statut": "pret" if ready else "echec",
        "audio_url": run.get("audio_url") if ready else None,
        "page_url": page_url if ready else None,
        # Saqr attend une durée entière en secondes (exemple du contrat: 540).
        "duree_s": int(round(run["duree_s"])) if ready and run.get("duree_s") is not None else None,
        "erreur": None if ready else (run.get("erreur") or "Échec de la production du podcast."),
    }
