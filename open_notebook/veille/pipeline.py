"""Production d'un podcast de veille: de la pièce publiée chez Saqr au MP3 vérifié, puis rappel.

Règles de fiabilité (recette du 30/09 au 02/10/2026):
- jamais le silence: toute issue (succès, échec, délai dépassé) produit un rappel vers Saqr, et un
  rappel non délivré est rejoué par reconcile.py;
- le contrôle de contenu refuse AVANT toute voix; un refus relance une nouvelle transcription;
- une panne du moteur de voix reprend l'épisode au clip près (resume_episode_id), sans tout refaire;
- « pret » n'est annoncé qu'après lecture du fichier (vrai MP3, durée mesurée).
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger
from surreal_commands import submit_command

from api.podcast_service import PodcastService
from open_notebook.database.repository import ensure_record_id, repo_query
from open_notebook.domain.notebook import Asset, Notebook, Source
from open_notebook.graphs.source import content_process
from open_notebook.podcasts.audio_paths import resolve_contained_audio_path
from open_notebook.utils.url_validation import validate_url
from open_notebook.veille import ingest, runs
from open_notebook.veille.audio_check import AudioInvalid, probe_mp3
from open_notebook.veille.saqr_client import (
    SaqrRejected,
    SaqrUnavailable,
    callback_payload,
    fetch_piece,
    post_callback,
)

EPISODE_PROFILE = os.environ.get("SAQR_VEILLE_EPISODE_PROFILE", "Veille-Eclaireurs")
SPEAKER_PROFILE = os.environ.get("SAQR_VEILLE_SPEAKER_PROFILE", "veille-eclaireurs")
GUARD_PREFIX = "Contrôle de contenu"
EXTRACTION_CONCURRENCY = 4
SOURCE_TIMEOUT_SECONDS = 150
POLL_SECONDS = 15
HEARTBEAT_SECONDS = 30
CALLBACK_WAITS = (5, 20, 60)
FETCH_WAITS = (5, 20, 60)
MAX_CALLBACK_ATTEMPTS_TOTAL = 20
DEFAULT_PUBLIC_URL = "https://diwan.ai-mpower.com"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_TIMEOUT_SECONDS = 45
ARXIV_WAITS = (0, 5, 15)


class ProductionFailed(Exception):
    """Échec définitif, avec un message destiné à Saqr (sans chemin ni secret)."""


# Mesuré les 04 et 05/10 sur 4 productions du panel de 13 à 20 minutes : 2 tentatives acceptées sur 15 (environ
# 13 %), donc 5 tentatives échouaient une fois sur deux. Une tentative refusée coûte environ une minute : 12
# tentatives tiennent dans les 70 minutes avec la voix (10 à 14 minutes).
DEFAULT_MAX_ATTEMPTS = 12

# Une voix déjà lancée (texte accepté par le contrôle) n'est jamais tuée à l'échéance des 70 minutes : le 05/10,
# l'essai 7 est devenu un épisode terminé à 16:04 alors que la production avait été déclarée en échec à 15:42,
# et Saqr n'a jamais vu cet épisode. Le plafond dur est l'échéance plus ce délai de grâce.
VOICE_GRACE_SECONDS = 90 * 60
JOB_FINISHED = ("completed", "failed", "canceled")


def max_attempts() -> int:
    try:
        return max(1, int(os.getenv("SAQR_VEILLE_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def sanitize(message: Optional[str]) -> str:
    """Une phrase actionnable, sans chemin interne."""
    first = (message or "").strip().splitlines()[0] if message else ""
    cleaned = " ".join(part for part in first.replace("\\", "/").split() if "/" not in part)
    return (cleaned or "Échec de la production du podcast.")[:300]


def _public_url() -> str:
    return os.getenv("DIWAN_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")


def audio_url_for(episode_id: str) -> str:
    return f"{_public_url()}/api/podcasts/episodes/{episode_id}/audio"


def page_url_for(run: Dict[str, Any]) -> Optional[str]:
    """Page qui renvoie vers les plateformes d'écoute.

    Modèle optionnel (SAQR_VEILLE_PAGE_URL_TEMPLATE) quand cette page existera chez Dīwān; d'ici là, la page
    de la veille chez Saqr reçue dans la demande (elle porte déjà le lecteur), jamais une valeur vide.
    """
    template = os.getenv("SAQR_VEILLE_PAGE_URL_TEMPLATE", "").strip()
    if template and run.get("episode"):
        return template.format(ref=run["ref"], episode_id=run["episode"])
    return run.get("page_url")


def time_left(run: Dict[str, Any]) -> float:
    return float(run.get("echeance") or 0) - time.time()


# --- lecture des sources -------------------------------------------------


async def _read(url: str, limiter: asyncio.Semaphore) -> Tuple[str, str]:
    """(titre, texte) lus par Diwan lui-même; ("", "") si la lecture échoue."""
    try:
        await validate_url(url, "source")
        async with limiter:
            extracted = await asyncio.wait_for(
                content_process({"content_state": {"url": url}}), SOURCE_TIMEOUT_SECONDS
            )
        extraction = extracted["extraction"]
        return (extraction.title or ""), (extraction.content or "").strip()
    except Exception as exc:  # noqa: BLE001 - une source illisible est classée, pas fatale
        logger.info(f"[veille] source illisible ({type(exc).__name__})")
        return "", ""


async def _linked_arxiv_pdf(source: Dict[str, Any]) -> Optional[str]:
    """PDF de l'article arXiv que annonce un post X (Saqr ne donne pas son adresse), ou None.

    Les chiffres de la veille viennent du corps de l'article: sans lui, le contrôle de contenu les refuse.
    """
    title = source.get("titre")
    query = ingest.arxiv_search_query(title)
    if not query:
        return None
    response = None
    for wait in ARXIV_WAITS:  # l'API d'arXiv est lente et limite le débit (ReadTimeout à 20 s, 429, 503 le 02/10)
        await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=ARXIV_TIMEOUT_SECONDS, follow_redirects=True) as client:
                response = await client.get(
                    ARXIV_API, params={"search_query": query, "max_results": 10},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
            break
        except httpx.HTTPError as exc:
            logger.info(f"[veille] recherche arXiv impossible ({type(exc).__name__})")
            response = None
    if response is None:
        return None
    ident = ingest.pick_arxiv_match(title, ingest.parse_arxiv_feed(response.text))
    return f"https://arxiv.org/pdf/{ident}" if ident else None


async def _read_source(
    source: Dict[str, Any], limiter: asyncio.Semaphore
) -> Tuple[str, str, str]:
    """(titre, texte de la page, texte de l'article complet s'il existe)."""
    url, pdf_url = ingest.source_urls(source)
    if not url:
        return "", "", ""
    if not pdf_url and ingest.is_short_form(source):
        pdf_url = await _linked_arxiv_pdf(source)
    tasks = [_read(url, limiter)] + ([_read(pdf_url, limiter)] if pdf_url else [])
    (title, text), *rest = await asyncio.gather(*tasks)
    return title, text, (rest[0][1] if rest else "")


async def read_sources(sources: List[Dict[str, Any]]) -> List[ingest.SourceResult]:
    limiter = asyncio.Semaphore(EXTRACTION_CONCURRENCY)
    reads = await asyncio.gather(*(_read_source(s, limiter) for s in sources))
    results: List[ingest.SourceResult] = []
    seen: Dict[int, str] = {}
    for source, (title, text, article) in zip(sources, reads):
        text, from_saqr_title = ingest.post_text(source, text)  # post X: repli sur le texte relevé par Saqr
        text, from_provided = ingest.prefer_provided(source, text)  # texte_complet de Saqr s'il est plus complet
        statut, detail = ingest.classify(source, title, text, seen, provided=from_provided)
        if statut in ingest.ALTERED and not from_provided:
            # Page illisible, paywall ou divergente: la source n'est inexploitable que si le texte_complet
            # de Saqr ne l'est pas non plus (demande de Saqr, 03/10).
            fallback = ingest.provided_text(source)
            if fallback:
                retry_statut, retry_detail = ingest.classify(source, title, fallback, seen, provided=True)
                if retry_statut in (ingest.INGEREE, ingest.REPRISE):
                    text, from_provided, statut, detail = fallback, True, retry_statut, retry_detail
        if from_provided and statut == ingest.INGEREE:
            detail += " (texte_complet fourni par Saqr)"
        elif from_saqr_title and statut == ingest.INGEREE:
            detail += " (texte du post relevé par Saqr, X ne se lit pas)"
        number = int(source.get("numero") or len(results) + 1)
        result = ingest.SourceResult(
            n=number, statut=statut, detail=detail, titre=str(source.get("titre") or ""),
            editeur=str(source.get("plateforme") or ""), meta=str(source.get("meta") or ""),
            url=ingest.source_urls(source)[0],
        )
        if statut in (ingest.INGEREE, ingest.REPRISE):
            result.texte = ingest.build_source_text(text, article)
        if statut == ingest.INGEREE:
            seen[number] = ingest.fold(text[:1200])
        results.append(result)
    return results


# --- notebook --------------------------------------------------------------


def source_title(result: ingest.SourceResult) -> str:
    return f"[Source {result.n}] {result.titre} ({result.editeur}, {result.meta})"


async def store_notebook(ref: str, results: List[ingest.SourceResult]) -> Tuple[str, List[Tuple[str, str]]]:
    """Enregistre les sources INTÉGRALES de la production dans son notebook, puis les en relit.

    Le podcast puise ses informations dans ce notebook (exigence d'Amine, 03/10): un notebook par
    veille, créé dès la lecture des sources, que la pièce soit publiée chez Saqr ou encore en brouillon.
    Idempotent: un rejeu réutilise le notebook et met à jour les sources de même titre.
    """
    name = f"Veille Saqr {ingest.date_of_ref(ref)}"
    found = await repo_query("SELECT id FROM notebook WHERE name = $name", {"name": name})
    if found:
        notebook = await Notebook.get(str(found[0]["id"]))
    else:
        notebook = Notebook(name=name, description=f"Sources intégrales de la veille Saqr du {ingest.french_date(ref)}.")
        await notebook.save()
    existing = {s.title: s for s in await notebook.get_sources(include_full_text=True)}
    titles: List[str] = []
    for result in results:
        title = source_title(result)
        titles.append(title)
        source = existing.get(title)
        if source is None:
            source = Source(title=title, full_text=result.texte, topics=[], asset=Asset(url=result.url or None))
            await source.save()
            await source.add_to_notebook(str(notebook.id))
        elif source.full_text != result.texte:
            source.full_text = result.texte
            await source.save()
        else:
            continue
        try:
            await source.vectorize()  # recherche dans le notebook; le podcast, lui, lit le texte intégral
        except Exception as exc:  # noqa: BLE001 - l'indexation est un confort, jamais un motif d'échec
            logger.warning(f"[veille] indexation de « {title[:40]} » impossible ({type(exc).__name__})")
    stored = {s.title: s.full_text or "" for s in await notebook.get_sources(include_full_text=True)}
    missing = [t for t in titles if not stored.get(t)]
    if missing:
        raise ProductionFailed(f"{len(missing)} source(s) absente(s) du notebook après enregistrement.")
    return str(notebook.id), [(t, stored[t]) for t in titles]


# --- génération ------------------------------------------------------------


async def _submit(
    name: str, content: str, resume_episode_id: Optional[str], briefing_suffix: Optional[str] = None
) -> str:
    return await PodcastService.submit_generation_job(
        episode_profile_name=EPISODE_PROFILE,
        speaker_profile_name=SPEAKER_PROFILE,
        episode_name=name,
        content=content,
        briefing_suffix=briefing_suffix,
        resume_episode_id=resume_episode_id,
    )


def refusal_feedback(error: str) -> str:
    """Consigne de l'essai suivant: le motif exact du refus du contrôle, à corriger point par point.

    Constaté le 03/10 : relancer à l'identique ne change rien (Hanae restait à 14-18 % sur 10 essais).
    """
    reasons = error[len(GUARD_PREFIX):].lstrip(" :")
    return (
        "La transcription précédente a été REFUSÉE par le contrôle de contenu. Écris une nouvelle "
        f"transcription qui corrige exactement ces points : {reasons}. Quand une part de parole est en "
        "défaut, REDISTRIBUE les répliques sans allonger l'épisode (même longueur totale, dans la fourchette "
        "de mots demandée) : transforme des répliques de l'intervenant qui en a trop en réactions de ceux "
        "qui n'en ont pas assez."
    )


async def _wait(job: str, run: Dict[str, Any]) -> Dict[str, Any]:
    while True:
        if time_left(run) <= 0 and (
            time_left(run) <= -VOICE_GRACE_SECONDS or not _is_resumable(await _episode_of(job))
        ):
            raise ProductionFailed("Délai de production dépassé pendant la génération de l'épisode.")
        try:
            status = await PodcastService.get_job_status(job)
        except Exception as exc:  # noqa: BLE001 - une lecture ratée se retente au prochain tour
            logger.info(f"[veille] statut du job illisible ({type(exc).__name__}), nouvel essai")
            status = {}
        if status.get("status") in ("completed", "failed", "canceled"):
            return status
        await asyncio.sleep(POLL_SECONDS)


async def _episode_of(job: str) -> Optional[Dict[str, Any]]:
    rows = await repo_query(
        "SELECT * FROM episode WHERE command = $job LIMIT 1", {"job": ensure_record_id(job)}
    )
    return rows[0] if rows else None


def _is_resumable(episode: Optional[Dict[str, Any]]) -> bool:
    """Un épisode ne se reprend au clip près que si sa transcription existe sur disque.

    Le 03/10, un épisode interrompu avant l'écriture de `transcript.json` a fait échouer les 5 essais en une
    minute (« Épisode non reprenable ») : sans transcription, on repart d'un épisode neuf.
    """
    output_dir = (episode or {}).get("output_dir")
    return bool(output_dir) and (Path(str(output_dir)) / "transcript.json").is_file()


async def _job_status(job: str) -> str:
    try:
        raw = (await PodcastService.get_job_status(job)).get("status")
        # Le statut est une énumération (`CommandStatus.COMPLETED`): str() en donne le nom, pas la valeur
        # « completed » comparée plus bas; l'adoption d'un épisode terminé après l'échec n'a jamais abouti.
        return str(getattr(raw, "value", raw) or "")
    except Exception as exc:  # noqa: BLE001 - un statut illisible ne doit jamais faire conclure à tort
        logger.info(f"[veille] statut du job illisible ({type(exc).__name__})")
        return ""


async def episodes_of_run(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Épisodes de la veille créés depuis la réception de la demande, du plus récent au plus ancien.

    Les tentatives de cette demande portent toutes le même nom; la marge de deux minutes couvre l'écart
    entre la réception et la création de la ligne. Les épisodes d'une demande antérieure (avant un
    « Régénérer ») sont exclus, car `echeance` repart à zéro à chaque relance.
    """
    started = float(run.get("echeance") or 0) - runs.deadline_seconds() - 120
    since = datetime.fromtimestamp(started, tz=timezone.utc).isoformat()
    return await repo_query(
        "SELECT * FROM episode WHERE name = $name AND created >= type::datetime($since) ORDER BY created DESC",
        {"name": f"Veille Saqr {ingest.date_of_ref(run['ref'])}", "since": since},
    )


async def voice_in_progress(run: Dict[str, Any]) -> bool:
    """Vrai si une tentative acceptée par le contrôle est en synthèse vocale: on la laisse finir."""
    if time_left(run) <= -VOICE_GRACE_SECONDS:
        return False
    for episode in await episodes_of_run(run):
        if episode.get("audio_file") or not episode.get("command") or not _is_resumable(episode):
            continue
        status = await _job_status(str(episode["command"]))
        if status and status not in JOB_FINISHED:
            return True
    return False


async def adopt_completed(run: Dict[str, Any]) -> bool:
    """Lie à la ligne l'épisode terminé qu'aucun essai n'a rattaché (échéance dépassée pendant la voix)."""
    for episode in await episodes_of_run(run):
        if not episode.get("audio_file") or not episode.get("command"):
            continue
        if await _job_status(str(episode["command"])) != "completed":
            continue
        await _finalize(run, str(episode["command"]))
        return True
    return False


async def _finalize(run: Dict[str, Any], job: str) -> None:
    """Lit le fichier produit; ne marque « pret » qu'après vérification du format et de la durée."""
    episode = await _episode_of(job)
    if not episode or not episode.get("audio_file"):
        raise ProductionFailed("L'épisode est terminé mais son fichier audio est introuvable.")
    path = resolve_contained_audio_path(episode["audio_file"])
    if path is None:
        raise ProductionFailed("Le fichier audio de l'épisode est hors du dossier autorisé.")
    try:
        info = await probe_mp3(path)
    except AudioInvalid as exc:
        raise ProductionFailed(f"Fichier audio refusé : {exc}") from exc
    episode_id = str(episode["id"])
    await runs.update_run(
        str(run["id"]), statut="pret", etape=None, erreur=None, episode=episode_id,
        audio_url=audio_url_for(episode_id), duree_s=info["duration_s"],
    )


async def generate(run: Dict[str, Any], content: str, name: str) -> None:
    """Génère l'épisode, avec relances: nouvelle transcription si le contrôle refuse, reprise au clip sinon."""
    run_id = str(run["id"])
    resume_episode_id: Optional[str] = None
    last_error = ""
    feedback: Optional[str] = None
    limit = max_attempts()
    for attempt in range(1, limit + 1):
        if time_left(run) <= 0:
            raise ProductionFailed("Délai de production dépassé avant une nouvelle tentative.")
        await runs.update_run(run_id, tentatives=attempt, etape=f"generation {attempt}/{limit}")
        job = await (
            _submit(name, content, resume_episode_id, briefing_suffix=feedback)
            if feedback
            else _submit(name, content, resume_episode_id)
        )
        await runs.update_run(run_id, job=job)
        status = await _wait(job, run)
        if status.get("status") == "completed":
            await _finalize(run, job)
            return
        last_error = status.get("error_message") or ""
        logger.info(f"[veille] {run['ref']} essai {attempt}/{limit} échoué : {sanitize(last_error)}")
        if last_error.startswith(GUARD_PREFIX):
            resume_episode_id = None  # le texte était mauvais: on repart d'une nouvelle transcription
            feedback = refusal_feedback(sanitize(last_error))  # ... qui corrige le motif du refus
            continue
        episode = await _episode_of(job)  # panne de voix: reprendre l'épisode au clip près
        resume_episode_id = str(episode["id"]) if episode and _is_resumable(episode) else None
    raise ProductionFailed(f"Échec après {limit} essais : {sanitize(last_error)}")


# --- orchestration ---------------------------------------------------------


async def _fetch_with_retries(ref: str) -> Dict[str, Any]:
    for wait in (*FETCH_WAITS, None):
        try:
            return await fetch_piece(ref)
        except SaqrUnavailable:
            if wait is None:
                raise
            await asyncio.sleep(wait)
    raise SaqrUnavailable("Saqr injoignable.")  # inatteignable, pour le typage


async def produce(run: Dict[str, Any]) -> None:
    run_id = str(run["id"])
    await runs.update_run(run_id, statut="en_cours", etape="recuperation")
    piece = await _fetch_with_retries(run["ref"])
    published = piece.get("content_hash") or piece.get("revision")
    if published and str(published) != str(run["revision"]):
        raise ProductionFailed("La veille publiée a changé depuis la demande (révision différente).")

    markdown = piece["contenu"]
    await runs.update_run(run_id, etape="sources")
    results = await read_sources(piece["sources"])
    rate = ingest.alteration_rate(results)
    usable = [r for r in results if r.statut in (ingest.INGEREE, ingest.REPRISE) and r.texte]
    try:
        notebook_id, stored = await store_notebook(run["ref"], usable)
    except ProductionFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - sans notebook, pas de podcast: le notebook est obligatoire
        raise ProductionFailed(f"Notebook de la production impossible : {sanitize(str(exc))}") from exc
    await runs.update_run(
        run_id,
        rapport={
            "notebook": notebook_id,
            "sources": len(results),
            "exploitables": sum(r.statut in (ingest.INGEREE, ingest.REPRISE) for r in results),
            "taux_alteration": round(rate, 3),
            "detail": [r.report() for r in results],
        },
    )
    # Saqr trie déjà ses signaux: le taux d'altération est rapporté, il ne bloque plus la production
    # (décision d'Amine, 08/10 : avec 9 sources, une seule défaillance dépassait l'ancien plafond de 10 %).
    # Sans aucune source exploitable, il n'y a rien à dire: pas de podcast.
    if not usable:
        raise ProductionFailed("Aucune source exploitable : pas de podcast.")
    content = ingest.build_content(run["ref"], markdown, stored)
    await generate(run, content, f"Veille Saqr {ingest.date_of_ref(run['ref'])}")


async def fail(run_id: str, message: str) -> None:
    await runs.update_run(run_id, statut="echec", etape=None, erreur=sanitize(message))


async def deliver_callback(run: Dict[str, Any], *, attempts: int = len(CALLBACK_WAITS)) -> bool:
    """Annonce l'issue à Saqr. Un échec de livraison laisse le rappel « a_envoyer » (rejoué par reconcile)."""
    if run.get("rappel_statut") == "envoye":
        return True
    run_id = str(run["id"])
    payload = callback_payload(run, page_url=page_url_for(run))
    done = int(run.get("rappel_tentatives") or 0)
    for index in range(attempts):
        done += 1
        try:
            await post_callback(payload)
        except SaqrRejected as exc:
            logger.error(f"[veille] rappel refusé par Saqr pour {run['ref']} : {sanitize(str(exc))}")
            await runs.update_run(run_id, rappel_statut="abandonne", rappel_tentatives=done)
            return False
        except SaqrUnavailable:
            if done >= MAX_CALLBACK_ATTEMPTS_TOTAL:
                await runs.update_run(run_id, rappel_statut="abandonne", rappel_tentatives=done)
                return False
            await runs.update_run(run_id, rappel_tentatives=done)
            if index < attempts - 1:
                await asyncio.sleep(CALLBACK_WAITS[min(index, len(CALLBACK_WAITS) - 1)])
            continue
        await runs.update_run(run_id, rappel_statut="envoye", rappel_tentatives=done)
        return True
    return False


def submit_orchestrator(ref: str) -> str:
    """Soumet la commande qui produit la veille; retourne l'identifiant du job."""
    # submit_command valide contre le registre local: la commande doit y être importée.
    import commands.saqr_commands  # noqa: F401

    job_id = submit_command("open_notebook", "saqr_veille_podcast", {"ref": ref})
    if not job_id:
        raise RuntimeError("Le job de production n'a pas pu être soumis.")
    return str(job_id)


async def _heartbeat(run_id: str) -> None:
    """Signale que la production est vivante; reconcile reprend une ligne dont le battement s'arrête."""
    while True:
        try:
            await runs.update_run(run_id, battement=time.time())
        except Exception as exc:  # noqa: BLE001 - un battement manqué ne doit pas tuer la production
            logger.warning(f"[veille] battement non enregistré ({type(exc).__name__})")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def run_veille(ref: str) -> None:
    """Produit la veille puis annonce l'issue. Idempotent: reprend une ligne déjà terminée sans la refaire."""
    run = await runs.get_run(ref)
    if run is None:
        logger.error(f"[veille] aucune ligne de suivi pour {ref}")
        return
    run_id = str(run["id"])
    if run["statut"] not in runs.TERMINAL:
        beat = asyncio.create_task(_heartbeat(run_id))
        try:
            left = time_left(run)
            if left <= 0:
                raise ProductionFailed("Délai de production dépassé avant le début.")
            # plafond dur = échéance + délai de grâce de la voix: les essais de texte s'arrêtent à l'échéance
            # (generate et _wait), une voix déjà lancée peut finir
            await asyncio.wait_for(produce(run), timeout=left + VOICE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            await fail(run_id, "Délai de production dépassé.")
        except ProductionFailed as exc:
            await fail(run_id, str(exc))
        except (SaqrRejected, SaqrUnavailable, ValueError) as exc:
            await fail(run_id, str(exc))
        except Exception:  # noqa: BLE001 - toute issue doit être annoncée, jamais le silence
            logger.exception(f"[veille] erreur inattendue pour {ref}")
            await fail(run_id, "Erreur interne pendant la production du podcast.")
        finally:
            beat.cancel()
    final = await runs.get_run(ref)
    if final:
        await deliver_callback(final)
