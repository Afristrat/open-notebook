"""Tests du pipeline et de la reprise du podcast de veille (open_notebook/veille/).

Ni base, ni réseau, ni voix: le suivi est un faux en mémoire (tests/veille_fakes.py) et les
étapes lourdes sont scénarisées. Ce qui est vérifié, ce sont les garanties de fiabilité demandées
par Saqr: jamais le silence, relance après refus du contrôle, reprise au clip après panne de voix,
« pret » seulement après vérification.
"""

import asyncio
import time

import pytest
from veille_fakes import FakeStore

from open_notebook.veille import ingest, pipeline, reconcile, runs
from open_notebook.veille.saqr_client import SaqrRejected, SaqrUnavailable

REF = "veille-2026-10-02"
GUARD_REFUSAL = "Contrôle de contenu : 1 écart(s) avec les sources, aucune voix générée."


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    monkeypatch.setattr(pipeline, "CALLBACK_WAITS", (0, 0, 0))
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0)
    monkeypatch.delenv("SAQR_VEILLE_PAGE_URL_TEMPLATE", raising=False)
    monkeypatch.delenv("SAQR_VEILLE_MAX_ATTEMPTS", raising=False)
    return fake


@pytest.fixture
def callbacks(monkeypatch):
    sent = []

    async def fake_post(payload):
        sent.append(payload)

    monkeypatch.setattr(pipeline, "post_callback", fake_post)
    return sent


class TestCallback:
    @pytest.mark.asyncio
    async def test_a_ready_episode_is_announced_with_its_audio(self, store, callbacks):
        run = store.add(statut="pret", audio_url="https://d/a", duree_s=681.8, episode="episode:1")
        assert await pipeline.deliver_callback(run) is True
        assert callbacks == [{
            "ref": REF, "revision": "rev1", "statut": "pret", "audio_url": "https://d/a",
            "page_url": None, "duree_s": 682, "erreur": None,
        }]
        assert store.by_ref(REF)["rappel_statut"] == "envoye"

    @pytest.mark.asyncio
    async def test_the_veille_page_received_from_saqr_is_sent_back_as_page_url(self, store, callbacks):
        run = store.add(statut="pret", audio_url="https://d/a", duree_s=540.4, page_url="https://saqr.ma/blog/veille/x")
        await pipeline.deliver_callback(run)
        assert callbacks[0]["page_url"] == "https://saqr.ma/blog/veille/x" and callbacks[0]["duree_s"] == 540

    @pytest.mark.asyncio
    async def test_a_failure_is_announced_with_its_reason_and_no_audio(self, store, callbacks):
        run = store.add(statut="echec", erreur="Délai de production dépassé.", audio_url="https://d/vieux")
        await pipeline.deliver_callback(run)
        assert callbacks[0]["statut"] == "echec"
        assert callbacks[0]["erreur"] == "Délai de production dépassé."
        assert callbacks[0]["audio_url"] is None and callbacks[0]["duree_s"] is None

    @pytest.mark.asyncio
    async def test_an_already_announced_run_is_not_announced_twice(self, store, callbacks):
        run = store.add(statut="pret", rappel_statut="envoye")
        assert await pipeline.deliver_callback(run) is True
        assert callbacks == []

    @pytest.mark.asyncio
    async def test_saqr_refusing_the_callback_stops_the_retries(self, store, monkeypatch):
        async def refuse(payload):
            raise SaqrRejected("refusé")

        monkeypatch.setattr(pipeline, "post_callback", refuse)
        run = store.add(statut="pret")
        assert await pipeline.deliver_callback(run) is False
        assert store.by_ref(REF)["rappel_statut"] == "abandonne"

    @pytest.mark.asyncio
    async def test_saqr_unreachable_leaves_the_callback_to_be_replayed(self, store, monkeypatch):
        async def down(payload):
            raise SaqrUnavailable("hors ligne")

        monkeypatch.setattr(pipeline, "post_callback", down)
        run = store.add(statut="echec", erreur="x")
        assert await pipeline.deliver_callback(run) is False
        row = store.by_ref(REF)
        assert row["rappel_statut"] == "a_envoyer" and row["rappel_tentatives"] == 3

    @pytest.mark.asyncio
    async def test_the_callback_is_abandoned_after_too_many_attempts(self, store, monkeypatch):
        async def down(payload):
            raise SaqrUnavailable("hors ligne")

        monkeypatch.setattr(pipeline, "post_callback", down)
        run = store.add(statut="echec", erreur="x", rappel_tentatives=pipeline.MAX_CALLBACK_ATTEMPTS_TOTAL - 1)
        await pipeline.deliver_callback(run, attempts=1)
        assert store.by_ref(REF)["rappel_statut"] == "abandonne"


class TestSaqrResponses:
    def test_a_refused_body_is_final_and_carries_the_reason_given_by_saqr(self):
        import httpx

        from open_notebook.veille import saqr_client

        response = httpx.Response(400, json={"ok": False, "erreur": "duree_invalide"})
        with pytest.raises(SaqrRejected, match="HTTP 400 : duree_invalide"):
            saqr_client._check(response, "Le rappel")

    def test_an_unknown_token_or_revision_is_final_but_a_503_is_replayed(self):
        import httpx

        from open_notebook.veille import saqr_client

        with pytest.raises(SaqrRejected):
            saqr_client._check(httpx.Response(404, json={"erreur": "revision_inconnue"}), "Le rappel")
        with pytest.raises(SaqrUnavailable):
            saqr_client._check(httpx.Response(503), "Le rappel")


def _script(monkeypatch, outcomes, episode_id=None):
    calls = {"submits": [], "finalized": []}

    async def fake_submit(name, content, resume_episode_id):
        calls["submits"].append(resume_episode_id)
        return f"command:{len(calls['submits'])}"

    async def fake_wait(job, run):
        return outcomes[len(calls["submits"]) - 1]

    async def fake_episode_of(job):
        return {"id": episode_id} if episode_id else None

    async def fake_finalize(run, job):
        calls["finalized"].append(job)

    monkeypatch.setattr(pipeline, "_submit", fake_submit)
    monkeypatch.setattr(pipeline, "_wait", fake_wait)
    monkeypatch.setattr(pipeline, "_episode_of", fake_episode_of)
    monkeypatch.setattr(pipeline, "_finalize", fake_finalize)
    return calls


class TestGenerate:
    @pytest.mark.asyncio
    async def test_a_content_guard_refusal_starts_a_new_transcription(self, store, monkeypatch):
        calls = _script(monkeypatch, [
            {"status": "failed", "error_message": GUARD_REFUSAL}, {"status": "completed"},
        ])
        await pipeline.generate(store.add(), "contenu", "Veille Saqr 2026-10-02")
        assert calls["submits"] == [None, None]
        assert calls["finalized"] == ["command:2"]
        assert store.by_ref(REF)["tentatives"] == 2

    @pytest.mark.asyncio
    async def test_a_voice_outage_resumes_the_same_episode_at_the_clip(self, store, monkeypatch):
        calls = _script(
            monkeypatch,
            [{"status": "failed", "error_message": "HTTP 500 du serveur de voix"}, {"status": "completed"}],
            episode_id="episode:abc",
        )
        await pipeline.generate(store.add(), "contenu", "n")
        assert calls["submits"] == [None, "episode:abc"]

    @pytest.mark.asyncio
    async def test_a_voice_outage_before_any_episode_exists_restarts_cleanly(self, store, monkeypatch):
        calls = _script(monkeypatch, [{"status": "failed", "error_message": "HTTP 500"}, {"status": "completed"}])
        await pipeline.generate(store.add(), "contenu", "n")
        assert calls["submits"] == [None, None]

    @pytest.mark.asyncio
    async def test_the_attempts_are_bounded_and_the_reason_is_kept(self, store, monkeypatch):
        monkeypatch.setenv("SAQR_VEILLE_MAX_ATTEMPTS", "2")
        calls = _script(monkeypatch, [{"status": "failed", "error_message": GUARD_REFUSAL}] * 2)
        with pytest.raises(pipeline.ProductionFailed, match="Échec après 2 essais"):
            await pipeline.generate(store.add(), "contenu", "n")
        assert len(calls["submits"]) == 2 and calls["finalized"] == []

    @pytest.mark.asyncio
    async def test_no_new_attempt_after_the_deadline(self, store, monkeypatch):
        calls = _script(monkeypatch, [{"status": "completed"}])
        run = store.add(echeance=time.time() - 1)
        with pytest.raises(pipeline.ProductionFailed, match="Délai"):
            await pipeline.generate(run, "contenu", "n")
        assert calls["submits"] == []


PIECE_SOURCES = [
    {"numero": n, "titre": f"Article numéro {n} sur les agents autonomes", "url": f"https://exemple.org/{n}",
     "plateforme": "RSS", "meta": "RSS, score 60", "article_url": None}
    for n in (1, 2, 3)
]


def _piece(**extra):
    return {"slug": REF, "contenu": "Fil conducteur de la veille.", "sources": PIECE_SOURCES, **extra}


def _scripted_reads(monkeypatch, readable):
    async def fake_read(source, limiter):
        if source["numero"] in readable:
            text = (f"{source['titre']}. Les agents autonomes progressent vite dans les entreprises "
                    "et les équipes doivent apprendre à les superviser avec méthode. ") * 6
            return source["titre"], text, ""
        return "", "", ""

    monkeypatch.setattr(pipeline, "_read_source", fake_read)


class TestProduce:
    @pytest.mark.asyncio
    async def test_the_content_carries_the_sources_diwan_read_and_the_episode_is_named_by_date(
        self, store, monkeypatch
    ):
        async def fake_fetch(ref):
            return _piece()

        captured = {}

        async def fake_generate(run, content, name):
            captured.update(content=content, name=name)

        monkeypatch.setattr(pipeline, "fetch_piece", fake_fetch)
        monkeypatch.setattr(pipeline, "generate", fake_generate)
        _scripted_reads(monkeypatch, {1, 2, 3})
        await pipeline.produce(store.add())
        assert captured["name"] == "Veille Saqr 2026-10-02"
        assert "=== SOURCES INGÉRÉES ===" in captured["content"]
        assert store.by_ref(REF)["rapport"]["taux_alteration"] == 0.0

    @pytest.mark.asyncio
    async def test_more_than_ten_percent_unusable_sources_means_no_podcast(self, store, monkeypatch):
        async def fake_fetch(ref):
            return _piece()

        async def never(*args):
            raise AssertionError("aucune génération attendue")

        monkeypatch.setattr(pipeline, "fetch_piece", fake_fetch)
        monkeypatch.setattr(pipeline, "generate", never)
        _scripted_reads(monkeypatch, {1})
        with pytest.raises(pipeline.ProductionFailed, match="inexploitables"):
            await pipeline.produce(store.add())
        report = store.by_ref(REF)["rapport"]
        assert report["exploitables"] == 1 and report["taux_alteration"] > ingest.MAX_ALTERATION

    @pytest.mark.asyncio
    async def test_a_changed_revision_is_refused(self, store, monkeypatch):
        async def fake_fetch(ref):
            return _piece(content_hash="autre")

        monkeypatch.setattr(pipeline, "fetch_piece", fake_fetch)
        with pytest.raises(pipeline.ProductionFailed, match="révision"):
            await pipeline.produce(store.add(revision="rev1"))


class TestReadSources:
    @pytest.mark.asyncio
    async def test_an_x_post_read_too_short_enters_the_corpus_with_the_text_saqr_captured(self, monkeypatch):
        title = ("160 CPU nodes. 30,000 CPU cores. 250 TB DRAM. 3 million sandbox instances per day, "
                 "380,000+ concurrently at peak. This is the infrastructure that ran every RL traini…")
        sources = [
            {"numero": 1, "titre": title, "url": "https://x.com/a/status/1", "plateforme": "X", "meta": "X, score 82"},
            {"numero": 2, "titre": "Article numéro 2 sur les agents autonomes", "url": "https://exemple.org/2",
             "plateforme": "RSS", "meta": "RSS"},
        ]

        async def fake_read(source, limiter):
            if source["numero"] == 1:
                return "", "X. It's what's happening", ""  # X ne se laisse pas lire: 25 caractères
            return "", "", ""  # une source qui n'est pas un post X, illisible

        monkeypatch.setattr(pipeline, "_read_source", fake_read)
        results = await pipeline.read_sources(sources, "Fil conducteur.")
        post, other = results
        assert post.statut == ingest.INGEREE and "380,000+" in post.texte and "relevé par Saqr" in post.detail
        assert other.statut == ingest.ECHEC and other.texte == ""


class TestRunVeille:
    @pytest.mark.asyncio
    async def test_a_known_failure_is_recorded_and_announced(self, store, callbacks, monkeypatch):
        async def boom(run):
            raise pipeline.ProductionFailed("60 % des sources sont inexploitables")

        monkeypatch.setattr(pipeline, "produce", boom)
        store.add()
        await pipeline.run_veille(REF)
        row = store.by_ref(REF)
        assert row["statut"] == "echec" and "inexploitables" in row["erreur"]
        assert callbacks[0]["statut"] == "echec" and "inexploitables" in callbacks[0]["erreur"]

    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_still_announced_never_silence(self, store, callbacks, monkeypatch):
        async def boom(run):
            raise KeyError("secret-interne")

        monkeypatch.setattr(pipeline, "produce", boom)
        store.add()
        await pipeline.run_veille(REF)
        assert callbacks[0]["statut"] == "echec"
        assert "secret-interne" not in callbacks[0]["erreur"]

    @pytest.mark.asyncio
    async def test_saqr_not_having_the_piece_is_a_reported_failure(self, store, callbacks, monkeypatch):
        async def missing(run):
            raise SaqrRejected("La lecture de la veille refusé par Saqr (HTTP 404).")

        monkeypatch.setattr(pipeline, "produce", missing)
        store.add()
        await pipeline.run_veille(REF)
        assert callbacks[0]["statut"] == "echec" and "404" in callbacks[0]["erreur"]

    @pytest.mark.asyncio
    async def test_a_production_running_past_the_deadline_is_cut_and_announced(self, store, callbacks, monkeypatch):
        async def slow(run):
            await asyncio.sleep(5)

        monkeypatch.setattr(pipeline, "produce", slow)
        store.add(echeance=time.time() + 0.2)
        await pipeline.run_veille(REF)
        assert callbacks[0]["statut"] == "echec" and "Délai" in callbacks[0]["erreur"]

    @pytest.mark.asyncio
    async def test_a_finished_run_is_not_produced_again_but_its_callback_is_replayed(
        self, store, callbacks, monkeypatch
    ):
        async def never(run):
            raise AssertionError("pas de nouvelle production")

        monkeypatch.setattr(pipeline, "produce", never)
        store.add(statut="pret", audio_url="https://d/a", duree_s=600.0)
        await pipeline.run_veille(REF)
        assert [c["statut"] for c in callbacks] == ["pret"]

    @pytest.mark.asyncio
    async def test_success_is_announced_as_ready(self, store, callbacks, monkeypatch):
        async def done(run):
            await runs.update_run(str(run["id"]), statut="pret", audio_url="https://d/a", duree_s=612.5)

        monkeypatch.setattr(pipeline, "produce", done)
        store.add()
        await pipeline.run_veille(REF)
        assert callbacks[0]["statut"] == "pret" and callbacks[0]["duree_s"] == 612


class TestReconcile:
    @pytest.mark.asyncio
    async def test_an_undelivered_callback_is_replayed(self, store, callbacks):
        store.add(statut="pret", audio_url="https://d/a", duree_s=600.0)
        await reconcile.reconcile_once()
        assert [c["statut"] for c in callbacks] == ["pret"]
        assert store.by_ref(REF)["rappel_statut"] == "envoye"

    @pytest.mark.asyncio
    async def test_a_run_past_its_deadline_is_concluded_and_announced(self, store, callbacks):
        store.add(statut="en_cours", echeance=time.time() - 5)
        await reconcile.reconcile_once()
        assert store.by_ref(REF)["statut"] == "echec"
        assert callbacks[0]["statut"] == "echec" and "Délai" in callbacks[0]["erreur"]

    @pytest.mark.asyncio
    async def test_a_dead_heartbeat_resubmits_the_production(self, store, callbacks, monkeypatch):
        monkeypatch.setattr(pipeline, "submit_orchestrator", lambda ref: "command:99")
        started_ago = 1000
        store.add(
            statut="en_cours", battement=time.time() - 500,
            echeance=time.time() + runs.deadline_seconds() - started_ago,
        )
        await reconcile.reconcile_once()
        row = store.by_ref(REF)
        assert row["reprises"] == 1 and row["job"] == "command:99" and row["statut"] == "en_cours"

    @pytest.mark.asyncio
    async def test_a_living_or_brand_new_run_is_left_alone(self, store, callbacks, monkeypatch):
        def never(ref):
            raise AssertionError("pas de reprise")

        monkeypatch.setattr(pipeline, "submit_orchestrator", never)
        store.add(ref="veille-2026-10-01", statut="en_cours", battement=time.time() - 10)
        store.add(ref=REF, statut="accepte")
        await reconcile.reconcile_once()
        assert callbacks == []

    @pytest.mark.asyncio
    async def test_too_many_interruptions_end_in_a_reported_failure(self, store, callbacks, monkeypatch):
        monkeypatch.setattr(pipeline, "submit_orchestrator", lambda ref: "command:1")
        store.add(
            statut="en_cours", reprises=reconcile.MAX_RESUBMITS, battement=time.time() - 500,
            echeance=time.time() + runs.deadline_seconds() - 1000,
        )
        await reconcile.reconcile_once()
        assert store.by_ref(REF)["statut"] == "echec"
        assert "interrompue" in callbacks[0]["erreur"]


X_POST = {
    "numero": 2,
    "titre": (
        "As agents tackle longer, more complex problems, controlling their execution becomes a challenge in "
        "itself. This work introduces agentic meta-reasoning, an inference-time harness."
    ),
    "url": "https://x.com/someone/status/2105721229055275175",
    "article_url": None,
}
ATOM_FEED = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2609.38147v1</id><title>Thinking Before Thinking: Meta-Reasoning</title>
<summary>As agents tackle longer, more complex problems, controlling their execution becomes a challenge.
We introduce agentic meta-reasoning, an inference-time harness.</summary></entry></feed>"""


class _FakeArxiv:
    """Remplace httpx.AsyncClient: renvoie un flux fixe, ou échoue comme un réseau coupé."""

    def __init__(self, body=ATOM_FEED, error=None):
        self.body, self.error = body, error

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        if self.error:
            raise self.error
        import httpx

        return httpx.Response(200, text=self.body, request=httpx.Request("GET", url))


class TestLinkedArxivPdf:
    """Le 02/10, le post X de la note 2 n'était pas lisible et son article (source des chiffres) manquait."""

    @pytest.mark.asyncio
    async def test_the_announced_article_is_found_and_read_in_full(self, monkeypatch):
        monkeypatch.setattr(pipeline.httpx, "AsyncClient", _FakeArxiv())
        read = []

        async def fake_read(url, limiter):
            read.append(url)
            return ("", "texte " + url)

        monkeypatch.setattr(pipeline, "_read", fake_read)
        title, text, article = await pipeline._read_source(X_POST, asyncio.Semaphore(1))
        assert read == [X_POST["url"], "https://arxiv.org/pdf/2609.38147v1"]
        assert article.endswith("2609.38147v1")

    @pytest.mark.asyncio
    async def test_no_article_is_taken_when_arxiv_answers_something_else(self, monkeypatch):
        other = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2606.07790v1</id>'
            "<title>Byzantine Cheap Talk in Coordination Games</title>"
            "<summary>We study adversarial resilience and topology effects in coordination.</summary></entry></feed>"
        )
        monkeypatch.setattr(pipeline.httpx, "AsyncClient", _FakeArxiv(body=other))
        assert await pipeline._linked_arxiv_pdf(X_POST) is None

    @pytest.mark.asyncio
    async def test_arxiv_unreachable_leaves_the_source_as_before(self, monkeypatch):
        monkeypatch.setattr(pipeline.httpx, "AsyncClient", _FakeArxiv(error=pipeline.httpx.ConnectError("down")))
        assert await pipeline._linked_arxiv_pdf(X_POST) is None

    @pytest.mark.asyncio
    async def test_other_sources_are_not_searched_on_arxiv(self, monkeypatch):
        called = []

        async def spy(source):
            called.append(source)

        async def fake_read(url, limiter):
            return ("", "texte")

        monkeypatch.setattr(pipeline, "_linked_arxiv_pdf", spy)
        monkeypatch.setattr(pipeline, "_read", fake_read)
        await pipeline._read_source({"url": "https://editeur.example/a", "titre": "x"}, asyncio.Semaphore(1))
        assert called == []
