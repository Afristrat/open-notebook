"""Format d'erreur stable de la facade consommateur.

Toute erreur renvoyee par /api/v1/consumers/* suit la meme enveloppe, pour que
le client puisse la traiter sans deviner la forme de la reponse:

    {
      "contractVersion": "1.0",
      "requestId": "...",
      "error": {
        "code": "SOURCE_NOT_AUTHORIZED",
        "message": "Cette source n'est pas accessible dans ce perimetre.",
        "retryable": false,
        "details": {}
      }
    }

Les messages sont rediges en francais et ne doivent jamais contenir de valeur
sensible: ni jeton, ni contenu documentaire, ni chemin interne. Les details
eventuels se limitent a des identifiants deja connus de l'appelant.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

CONTRACT_VERSION = "1.0"

UNAUTHENTICATED = "UNAUTHENTICATED"
FORBIDDEN = "FORBIDDEN"
CORPUS_NOT_FOUND = "CORPUS_NOT_FOUND"
SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
SOURCE_NOT_AUTHORIZED = "SOURCE_NOT_AUTHORIZED"
SOURCE_NOT_READY = "SOURCE_NOT_READY"
INVALID_SOURCE_TYPE = "INVALID_SOURCE_TYPE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
INGESTION_FAILED = "INGESTION_FAILED"
EXTRACTION_FAILED = "EXTRACTION_FAILED"
OCR_FAILED = "OCR_FAILED"
EMBEDDING_FAILED = "EMBEDDING_FAILED"
EMBEDDING_MODEL_UNAVAILABLE = "EMBEDDING_MODEL_UNAVAILABLE"
EMBEDDING_DIMENSION_MISMATCH = "EMBEDDING_DIMENSION_MISMATCH"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
RATE_LIMITED = "RATE_LIMITED"
SERVICE_BUSY = "SERVICE_BUSY"
CONTRACT_VERSION_UNSUPPORTED = "CONTRACT_VERSION_UNSUPPORTED"
INVALID_REQUEST = "INVALID_REQUEST"

# (statut HTTP, message par defaut, rejouable)
_CATALOG: Dict[str, tuple] = {
    UNAUTHENTICATED: (401, "Authentification requise.", False),
    FORBIDDEN: (403, "Ce perimetre n'est pas accessible avec cette identite.", False),
    CORPUS_NOT_FOUND: (404, "Ce corpus est introuvable dans ce perimetre.", False),
    SOURCE_NOT_FOUND: (404, "Cette source est introuvable dans ce perimetre.", False),
    SOURCE_NOT_AUTHORIZED: (
        403,
        "Cette source n'est pas accessible dans ce perimetre.",
        False,
    ),
    SOURCE_NOT_READY: (
        409,
        "Cette source n'est pas encore prete: extraction ou vectorisation incomplete.",
        True,
    ),
    INVALID_SOURCE_TYPE: (
        415,
        "Ce type de fichier n'est pas accepte par le contrat.",
        False,
    ),
    FILE_TOO_LARGE: (413, "Ce fichier depasse la taille maximale autorisee.", False),
    INGESTION_FAILED: (500, "L'import a echoue.", True),
    EXTRACTION_FAILED: (500, "L'extraction du contenu a echoue.", True),
    OCR_FAILED: (500, "La reconnaissance optique de caracteres a echoue.", True),
    EMBEDDING_FAILED: (500, "La vectorisation a echoue.", True),
    EMBEDDING_MODEL_UNAVAILABLE: (
        503,
        "Le modele de vectorisation n'est pas disponible.",
        True,
    ),
    EMBEDDING_DIMENSION_MISMATCH: (
        409,
        "Les vecteurs du corpus et ceux de la requete n'ont pas la meme dimension. "
        "Une reindexation est necessaire avant de rechercher.",
        False,
    ),
    INSUFFICIENT_EVIDENCE: (200, "Aucun passage ne satisfait le seuil demande.", False),
    RATE_LIMITED: (429, "Trop de requetes: reessayez plus tard.", True),
    SERVICE_BUSY: (503, "Le service est momentanement sature.", True),
    CONTRACT_VERSION_UNSUPPORTED: (
        400,
        "La version de contrat demandee n'est pas supportee.",
        False,
    ),
    INVALID_REQUEST: (422, "La requete est invalide.", False),
}


class ConsumerAPIError(HTTPException):
    """Erreur de la facade, portee par le format d'enveloppe stable.

    Herite de HTTPException pour rester compatible avec la gestion d'erreurs
    de FastAPI, mais son rendu passe toujours par consumer_error_response().
    """

    def __init__(
        self,
        code: str,
        *,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
    ) -> None:
        default_status, default_message, retryable = _CATALOG.get(
            code, (500, "Erreur inattendue.", False)
        )
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.message = message or default_message
        super().__init__(
            status_code=status_code or default_status, detail=self.message
        )


def error_payload(
    code: str,
    request_id: str,
    *,
    message: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construit l'enveloppe d'erreur du contrat."""
    _, default_message, retryable = _CATALOG.get(
        code, (500, "Erreur inattendue.", False)
    )
    return {
        "contractVersion": CONTRACT_VERSION,
        "requestId": request_id,
        "error": {
            "code": code,
            "message": message or default_message,
            "retryable": retryable,
            "details": details or {},
        },
    }


def consumer_error_response(request: Request, exc: ConsumerAPIError) -> JSONResponse:
    """Rend une ConsumerAPIError dans le format d'enveloppe du contrat."""
    request_id = getattr(request.state, "request_id", "") or ""
    headers = {"X-Request-Id": request_id} if request_id else None
    if exc.code == UNAUTHENTICATED:
        headers = dict(headers or {})
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(
            exc.code, request_id, message=exc.message, details=exc.details
        ),
        headers=headers,
    )
