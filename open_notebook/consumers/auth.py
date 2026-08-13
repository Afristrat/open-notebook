"""Authentification interservice de la facade consommateur.

Principe: Diwan ne detient jamais le jeton en clair. La configuration ne porte
que des empreintes SHA-256. Un jeton presente par le client est hache puis
compare en temps constant aux empreintes connues.

Consequences directes:
  - la valeur du jeton n'existe ni dans le code, ni dans l'environnement du
    conteneur, ni dans les journaux, ni dans la documentation;
  - la rotation sans interruption se fait en declarant deux empreintes pour la
    meme organisation, le temps que le client bascule;
  - la revocation d'un consommateur se fait en retirant son empreinte, sans
    toucher aux donnees.

Format de configuration (variable DIWAN_CONSUMER_TOKENS, ou fichier via
DIWAN_CONSUMER_TOKENS_FILE), entrees separees par des virgules ou des
retours a la ligne:

    <consommateur>:<identifiant d'organisation externe>:<empreinte sha256>

L'organisation autorisee est donc TOUJOURS derivee du jeton presente. Un
identifiant d'organisation fourni dans le corps d'une requete n'est jamais une
preuve d'autorisation: il est seulement compare au perimetre du jeton.
"""

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Dict, List, Optional, Tuple

from fastapi import Header, Request
from loguru import logger

from open_notebook.consumers.errors import (
    FORBIDDEN,
    RATE_LIMITED,
    UNAUTHENTICATED,
    ConsumerAPIError,
)
from open_notebook.utils.encryption import get_secret_from_env

_TOKENS_ENV = "DIWAN_CONSUMER_TOKENS"


@dataclass(frozen=True)
class ConsumerIdentity:
    """Identite authentifiee: le perimetre autorise, derive du seul jeton."""

    consumer_id: str
    organization_external_id: str

    def __str__(self) -> str:
        return f"{self.consumer_id}/{self.organization_external_id}"


def _parse_token_config(raw: str) -> Dict[str, ConsumerIdentity]:
    """Transforme la configuration en table empreinte -> identite.

    Les entrees invalides sont ignorees avec un avertissement qui ne cite que
    leur position, jamais leur contenu.
    """
    table: Dict[str, ConsumerIdentity] = {}
    entries = [e.strip() for e in raw.replace("\n", ",").split(",")]
    for position, entry in enumerate(entries, start=1):
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            logger.warning(
                f"[consumers] entree de configuration ignoree (position {position}): "
                "format attendu consommateur:organisation:empreinte"
            )
            continue
        consumer_id, organization_id, digest = (p.strip() for p in parts)
        digest = digest.lower()
        if not consumer_id or not organization_id or len(digest) != 64:
            logger.warning(
                f"[consumers] entree de configuration ignoree (position {position}): "
                "empreinte SHA-256 attendue en hexadecimal sur 64 caracteres"
            )
            continue
        table[digest] = ConsumerIdentity(consumer_id, organization_id)
    return table


def load_identities() -> Dict[str, ConsumerIdentity]:
    """Charge la table des empreintes autorisees.

    Relue a chaque appel: retirer une empreinte de la configuration revoque le
    consommateur au redemarrage du service, sans migration ni purge de donnees.
    """
    raw = get_secret_from_env(_TOKENS_ENV) or ""
    return _parse_token_config(raw)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _match_identity(
    token: str, table: Dict[str, ConsumerIdentity]
) -> Optional[ConsumerIdentity]:
    """Compare l'empreinte du jeton en temps constant contre toutes les entrees.

    La boucle parcourt la table entiere meme apres une correspondance, pour ne
    pas transformer la position d'une entree en canal temporel.
    """
    presented = _hash_token(token)
    found: Optional[ConsumerIdentity] = None
    for digest, identity in table.items():
        if secrets.compare_digest(presented, digest):
            found = identity
    return found


class _RateLimiter:
    """Limitation de frequence par identite, sur fenetre glissante.

    Volontairement en memoire du processus: Diwan tourne en un seul conteneur
    applicatif (verifie par l'audit), et une dependance supplementaire ne se
    justifie pas tant que ce n'est pas le cas.
    """

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: Dict[str, List[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> Tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._calls.get(key, []) if now - t < self.window_seconds]
            if len(recent) >= self.max_calls:
                self._calls[key] = recent
                retry_after = int(self.window_seconds - (now - recent[0])) + 1
                return False, retry_after
            recent.append(now)
            self._calls[key] = recent
            return True, 0


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"[consumers] {name} illisible, valeur par defaut {default}")
        return default
    return value if value > 0 else default


_READ_LIMITER = _RateLimiter(
    max_calls=_int_from_env("DIWAN_CONSUMER_RATE_LIMIT", 120), window_seconds=60
)
_INGEST_LIMITER = _RateLimiter(
    max_calls=_int_from_env("DIWAN_CONSUMER_INGEST_RATE_LIMIT", 10), window_seconds=60
)


async def authenticate(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> ConsumerIdentity:
    """Dependance d'authentification de la facade.

    Independante du middleware historique PasswordAuthMiddleware: elle refuse
    meme quand celui-ci laisse tout passer (cas ou OPEN_NOTEBOOK_PASSWORD n'est
    pas defini). C'est la seule porte d'entree de la facade.
    """
    table = load_identities()
    if not table:
        logger.error(
            "[consumers] aucune empreinte de jeton configuree: la facade refuse "
            "toutes les requetes"
        )
        raise ConsumerAPIError(UNAUTHENTICATED)

    if not authorization:
        raise ConsumerAPIError(UNAUTHENTICATED)

    try:
        scheme, token = authorization.split(" ", 1)
    except ValueError:
        raise ConsumerAPIError(UNAUTHENTICATED)

    if scheme.lower() != "bearer" or not token.strip():
        raise ConsumerAPIError(UNAUTHENTICATED)

    identity = _match_identity(token.strip(), table)
    if identity is None:
        raise ConsumerAPIError(UNAUTHENTICATED)

    allowed, retry_after = _READ_LIMITER.check(str(identity))
    if not allowed:
        raise ConsumerAPIError(
            RATE_LIMITED, details={"retryAfterSeconds": retry_after}
        )

    request.state.consumer_identity = identity
    return identity


async def authenticate_for_ingestion(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> ConsumerIdentity:
    """Meme porte, avec la limite de frequence propre aux imports."""
    identity = await authenticate(request, authorization)
    allowed, retry_after = _INGEST_LIMITER.check(f"ingest:{identity}")
    if not allowed:
        raise ConsumerAPIError(
            RATE_LIMITED, details={"retryAfterSeconds": retry_after}
        )
    return identity


def require_consumer(identity: ConsumerIdentity, expected_consumer: str) -> None:
    """Verifie que l'identite appartient bien au consommateur attendu.

    Un jeton emis pour un autre consommateur ne doit pas ouvrir cette facade,
    meme si son empreinte est valide.
    """
    if identity.consumer_id != expected_consumer:
        raise ConsumerAPIError(FORBIDDEN)
