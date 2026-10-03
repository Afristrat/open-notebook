"""Tests de l'ingestion d'une veille Saqr (open_notebook/veille/ingest.py).

Fonctions pures: aucun réseau, aucune base. Les cas viennent de la recette du 30/09 au 02/10/2026.
"""

import pytest

from open_notebook.podcasts import content_guard as cg
from open_notebook.veille import ingest

ARXIV = {
    "numero": 3,
    "titre": "Argo-Bench: Evaluating Data Agents on Enterprise-Scale Workflows",
    "url": "http://arxiv.org/abs/2610.02122v1",
    "plateforme": "ARXIV",
    "meta": "ARXIV, score 62, 1 oct. 2026",
    "article_url": None,
}
X_POST = {
    "numero": 1,
    "titre": "New agent harness results on long horizon tasks",
    "url": "https://x.com/someone/status/123",
    "plateforme": "X",
    "meta": "X, score 70",
    "article_url": None,
}
ARTICLE = (
    "Argo-Bench evaluates data agents on enterprise-scale workflows with long horizon tasks. " * 6
)


class TestDates:
    def test_ref_to_iso_and_french_date(self):
        assert ingest.date_of_ref("veille-2026-10-02") == "2026-10-02"
        assert ingest.french_date("veille-2026-10-02") == "2 octobre 2026"
        assert ingest.french_date("veille-2026-02-01") == "1 février 2026"

    def test_invalid_ref_is_refused(self):
        with pytest.raises(ValueError):
            ingest.date_of_ref("veille-demain")


class TestSourceUrls:
    def test_arxiv_abstract_page_comes_with_the_full_pdf(self):
        url, pdf = ingest.source_urls(ARXIV)
        assert url == "http://arxiv.org/abs/2610.02122v1"
        assert pdf == "https://arxiv.org/pdf/2610.02122v1"

    def test_publisher_address_resolved_by_saqr_is_preferred(self):
        source = {"url": "https://news.google.com/rss/articles/xyz", "article_url": "https://editeur.example/a"}
        assert ingest.source_urls(source) == ("https://editeur.example/a", None)


class TestClassify:
    def test_nothing_read_is_a_failure(self):
        assert ingest.classify(ARXIV, "", "", {})[0] == ingest.ECHEC

    def test_captcha_wall_is_a_failure(self):
        text = "This page maybe requiring CAPTCHA, please make sure you are authorized."
        assert ingest.classify(ARXIV, "", text, {})[0] == ingest.ECHEC

    def test_paywall_marker_counts_only_on_a_short_text(self):
        short = "Abonnez-vous pour lire la suite de cet article réservé. " * 8
        assert ingest.classify(ARXIV, "Argo-Bench", short, {})[0] == ingest.PAYWALL
        long_article = ARTICLE + " Abonnez-vous à la newsletter. " + ARTICLE * 6
        assert len(long_article) > 3000
        assert ingest.classify(ARXIV, "Argo-Bench", long_article, {})[0] == ingest.INGEREE

    def test_a_short_post_is_valid_on_x_but_not_elsewhere(self):
        text = "New agent harness results on long horizon tasks: a short thread about the numbers we measured."
        assert 80 <= len(text) < ingest.MIN_TEXT_CHARS
        assert ingest.classify(X_POST, "", text, {})[0] == ingest.INGEREE
        assert ingest.classify(ARXIV, "", text, {})[0] == ingest.ECHEC

    def test_a_page_that_does_not_match_the_announced_title_diverges(self):
        text = "Recette de cuisine : mélanger la farine et le sucre dans un grand saladier. " * 8
        assert ingest.classify(ARXIV, "Cuisine", text, {})[0] == ingest.DIVERGENT

    def test_a_copy_of_an_earlier_source_is_a_reprise_not_an_alteration(self):
        seen = {8: ingest.fold(ARTICLE[:1200])}
        statut, detail = ingest.classify(ARXIV, "Argo-Bench", ARTICLE, seen)
        assert statut == ingest.REPRISE
        assert "8" in detail
        assert ingest.REPRISE not in ingest.ALTERED

    def test_a_good_article_is_ingested(self):
        assert ingest.classify(ARXIV, "Argo-Bench", ARTICLE, {})[0] == ingest.INGEREE


class TestPostText:
    LONG_TITLE = ("160 CPU nodes. 30,000 CPU cores. 250 TB DRAM. 3 million sandbox instances per day, "
                  "380,000+ concurrently at peak. This is the infrastructure that ran every RL traini…")

    def test_an_x_post_read_too_short_falls_back_on_the_text_saqr_captured(self):
        # Régression du 02/10: 71 caractères lus contre le texte complet relevé par Saqr.
        source = {**X_POST, "titre": self.LONG_TITLE}
        text, from_title = ingest.post_text(source, "X. It's what's happening, log in")
        assert from_title and text == self.LONG_TITLE
        assert ingest.classify(source, "", text, {})[0] == ingest.INGEREE

    def test_an_x_post_read_in_full_keeps_what_dwan_read(self):
        source = {**X_POST, "titre": "Court titre"}
        assert ingest.post_text(source, "Le texte complet du post lu par Diwan, plus long que le titre.") == (
            "Le texte complet du post lu par Diwan, plus long que le titre.", False)

    def test_the_fallback_never_applies_to_a_source_that_is_not_an_x_post(self):
        source = {**ARXIV, "titre": "Un titre très long " * 20}
        assert ingest.post_text(source, "") == ("", False)

    def test_both_x_hosts_are_short_form(self):
        assert ingest.is_short_form({"url": "https://twitter.com/a/status/1"})
        assert ingest.is_short_form({"url": "https://www.x.com/a/status/1"})
        assert not ingest.is_short_form({"url": "https://arxiv.org/abs/1"})


class TestProvidedText:
    """`texte_complet` de Saqr (livré le 03/10, demande 4) : lu quand la page ne l'est pas."""

    POST = ("Muse Spark 1.1 et 1.2 : Meta publie deux versions de son modèle, avec des gains mesurés sur "
            "les tâches longues. " * 8)
    FEED = "Résumé du flux : l'éditeur annonce une mise à jour de sa plateforme d'agents, sans détail chiffré."

    def test_the_text_of_saqr_replaces_a_shorter_page(self):
        source = {**X_POST, "texte_complet": self.POST}
        text, from_saqr = ingest.prefer_provided(source, "X. It's what's happening, log in")
        assert from_saqr and text == self.POST.strip()
        assert "1.1" in text and "1.2" in text

    def test_a_longer_page_read_by_diwan_is_kept(self):
        source = {**ARXIV, "texte_complet": "Résumé court de Saqr."}
        assert ingest.prefer_provided(source, ARTICLE) == (ARTICLE, False)

    def test_no_text_of_saqr_changes_nothing(self):
        assert ingest.prefer_provided(ARXIV, "page") == ("page", False)
        assert ingest.prefer_provided({**ARXIV, "texte_complet": None}, "page") == ("page", False)

    FEED_SOURCE = {**ARXIV, "titre": "Plateforme agents mise à jour"}

    def test_a_short_feed_summary_is_usable_only_when_it_comes_from_saqr(self):
        assert ingest.classify(self.FEED_SOURCE, "", self.FEED, {})[0] == ingest.ECHEC
        assert ingest.classify(self.FEED_SOURCE, "", self.FEED, {}, provided=True)[0] == ingest.INGEREE

    def test_a_signal_of_saqr_is_not_a_page_so_no_paywall_to_look_for(self):
        text = "Abonnez-vous pour lire : l'éditeur résume ici sa mise à jour de plateforme d'agents en trois points."
        assert ingest.classify(self.FEED_SOURCE, "", text, {})[0] == ingest.PAYWALL
        assert ingest.classify(self.FEED_SOURCE, "", text, {}, provided=True)[0] == ingest.INGEREE


class TestMainText:
    def test_links_images_and_consent_banners_are_removed(self):
        raw = (
            "![logo](https://x.example/logo.png)\n"
            "We use cookies to improve your experience on this site, accept all cookies please.\n"
            "The study shows that [agents](https://x.example/a) fail on long tasks, which matters a lot.\n"
        )
        text = ingest.main_text(raw)
        assert "cookies" not in text
        assert "https://" not in text
        assert "agents fail on long tasks" in text

    def test_the_whole_text_is_kept_without_any_truncation(self):
        raw = ("Une phrase assez longue pour être conservée par le filtre de lignes courtes. " * 2000)
        assert len(ingest.main_text(raw)) > 100_000


class TestSourceText:
    def test_the_page_is_given_in_full(self):
        page = "Une phrase assez longue pour être conservée par le filtre de lignes courtes. " * 500
        assert len(ingest.build_source_text(page)) >= len(page.strip()) - 1

    def test_the_full_article_follows_the_page_when_it_is_distinct(self):
        article = "On ProgramBench, the controller reaches 71.5 percent against 58.0 for Codex in our runs."
        text = ingest.build_source_text(ARTICLE, article)
        assert text.startswith(ARTICLE.strip()[:40]) and "[Article complet]" in text and "71.5" in text

    def test_nothing_is_appended_without_an_article_or_when_it_is_already_in_the_page(self):
        assert "[Article complet]" not in ingest.build_source_text(ARTICLE, "")
        assert "[Article complet]" not in ingest.build_source_text(ARTICLE, ARTICLE)


class TestContent:
    def _result(self, number, statut, text="Texte de la source."):
        return ingest.SourceResult(
            n=number, statut=statut, detail="", titre=f"Titre {number}", editeur="ARXIV",
            meta="ARXIV, score 60", url="https://exemple.org", texte=text,
        )

    def test_the_corpus_is_the_sources_read_in_the_notebook_and_the_guard_can_read_it(self):
        sources = [
            ("[Source 1] Titre 1 (ARXIV, score 60)", "Premier texte lu dans le notebook."),
            ("[Source 3] Titre 3 (ARXIV, score 60)", "Troisième texte."),
        ]
        content = ingest.build_content("veille-2026-10-02", "Corps de la veille.\n\n[^1]: note", sources)
        corpus = cg.extract_corpus(content)
        assert "Premier texte" in corpus and "Troisième texte" in corpus
        assert "[Source 1] Titre 1" in corpus
        assert "Date de la veille : 2 octobre 2026" in content
        assert "[^1]" not in content

    def test_alteration_rate(self):
        results = [self._result(1, ingest.INGEREE), self._result(2, ingest.ECHEC),
                   self._result(3, ingest.PAYWALL), self._result(4, ingest.REPRISE)]
        assert ingest.alteration_rate(results) == 0.5
        assert ingest.alteration_rate([]) == 1.0

    def test_the_report_never_carries_the_texts(self):
        assert "texte" not in self._result(1, ingest.INGEREE).report()


class TestRegressionOfTheDay:
    """Le 02/10, 71,5 % et 67,2 % venaient du CORPS d'un article, pas de sa page de résumé."""

    LINE = "Le contrôleur atteint 71,5 % sur ProgramBench."
    NARRATIVE = "Un contrôleur atteint 71,5 % sur ProgramBench."
    ABSTRACT = "Agentic meta-reasoning lets an agent decide which work to pursue, reuse or stop. " * 4
    ARTICLE_TEXT = "On ProgramBench, the controller reaches 71.5 percent with the new harness in our runs."

    def _guard(self, article_text):
        text = ingest.build_source_text(self.ABSTRACT, article_text)
        content = ingest.build_content("veille-2026-10-02", self.NARRATIVE, [("[Source 2] Meta (X, X)", text)])
        return cg.check_transcript([self.LINE], cg.extract_corpus(content)).violations

    def test_refused_when_only_the_abstract_was_read(self):
        assert any(v.kind == "nombre_absent" for v in self._guard(""))

    def test_accepted_once_the_full_article_extract_is_in_the_corpus(self):
        assert self._guard(self.ARTICLE_TEXT) == []


POST_TEXT = (
    "As agents tackle longer, more complex problems, controlling their execution becomes a challenge in "
    "itself. This work introduces agentic meta-reasoning, an inference-time harness that explicitly reasons "
    "about which work to pursue, reuse, or stop."
)
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2606.07790v1</id>
    <title>Byzantine Cheap Talk: Adversarial Resilience in LLM Coordination Games</title>
    <summary>We study adversarial agents in coordination games with cheap talk.</summary>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2609.38147v1</id>
    <title>Thinking Before Thinking: Scaling Agentic Inference Through Meta-Reasoning</title>
    <summary>As agents tackle longer, more complex problems, controlling their execution becomes a challenge.
    We introduce agentic meta-reasoning, an inference-time harness that reasons about which work to
    pursue, reuse, or stop.</summary>
  </entry>
</feed>"""


class TestLinkedArxivArticle:
    """Le 02/10, Saqr ne donne pas l'article d'un post X: Dīwān le retrouve, sans jamais en prendre un au hasard."""

    def test_query_puts_hyphenated_expressions_first(self):
        query = ingest.arxiv_search_query(POST_TEXT)
        assert query is not None
        assert query.startswith('all:"')
        assert 'all:"meta-reasoning"' in query and 'all:"inference-time"' in query
        assert " OR " not in query and query.count(" AND ") == 2

    def test_a_post_too_short_to_identify_a_paper_gives_no_query(self):
        assert ingest.arxiv_search_query("New results on benchmarks") is None
        assert ingest.arxiv_search_query(None) is None

    def test_feed_is_parsed_into_identifier_title_and_summary(self):
        entries = ingest.parse_arxiv_feed(FEED)
        assert [e["id"] for e in entries] == ["2606.07790v1", "2609.38147v1"]
        assert entries[1]["title"].startswith("Thinking Before Thinking")

    def test_unreadable_feed_gives_no_entry(self):
        assert ingest.parse_arxiv_feed("pas du xml") == []

    def test_a_feed_with_a_dtd_is_refused(self):
        hostile = '<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "aaaa">]><feed xmlns="http://www.w3.org/2005/Atom"/>'
        assert ingest.parse_arxiv_feed(hostile) == []

    def test_the_paper_the_post_announces_is_picked(self):
        assert ingest.pick_arxiv_match(POST_TEXT, ingest.parse_arxiv_feed(FEED)) == "2609.38147v1"

    def test_no_paper_is_taken_when_none_matches(self):
        unrelated = ingest.parse_arxiv_feed(FEED)[:1]
        assert ingest.pick_arxiv_match(POST_TEXT, unrelated) is None
        assert ingest.pick_arxiv_match(POST_TEXT, []) is None
