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

    def test_the_length_is_bounded(self):
        raw = ("Une phrase assez longue pour être conservée par le filtre de lignes courtes. " * 200)
        assert len(ingest.main_text(raw)) <= ingest.MAX_SOURCE_CHARS


class TestExcerpts:
    NARRATIVE = "Un contrôleur atteint 71,5 % sur ProgramBench contre 58,0 % pour Codex."

    def test_sentences_carrying_the_numbers_of_the_veille_are_kept(self):
        article = (
            "On ProgramBench, the controller reaches 71.5 percent against 58.0 for Codex in our runs. "
            "Related work on agents has been extensive over the last years of research in the field."
        )
        excerpt = ingest.article_excerpt(article, self.NARRATIVE)
        assert "71.5" in excerpt
        assert "Related work" not in excerpt

    def test_nothing_is_added_when_no_number_matches(self):
        assert ingest.article_excerpt("A long sentence without any figure that matters here at all.", self.NARRATIVE) == ""

    def test_the_excerpt_stays_within_its_budget(self):
        sentence = "The controller reaches 71.5 percent on the benchmark in this experiment %d. "
        article = "".join(sentence % i for i in range(200))
        assert len(ingest.article_excerpt(article, self.NARRATIVE)) <= ingest.EXTRACT_CHARS

    def test_the_source_text_marks_the_extract_only_when_there_is_one(self):
        article = "On ProgramBench, the controller reaches 71.5 percent against 58.0 for Codex in our runs."
        with_extract = ingest.build_source_text(ARTICLE, self.NARRATIVE, article)
        assert "[Extraits de l'article complet]" in with_extract and "71.5" in with_extract
        assert "[Extraits" not in ingest.build_source_text(ARTICLE, self.NARRATIVE, "")


class TestContent:
    def _result(self, number, statut, text="Texte de la source."):
        return ingest.SourceResult(
            n=number, statut=statut, detail="", titre=f"Titre {number}", editeur="ARXIV",
            meta="ARXIV, score 60", url="https://exemple.org", texte=text,
        )

    def test_only_usable_sources_enter_the_corpus_and_the_guard_can_read_it(self):
        results = [
            self._result(1, ingest.INGEREE, "Premier texte lu par Diwan."),
            self._result(2, ingest.ECHEC, "Ne doit pas entrer."),
            self._result(3, ingest.REPRISE, "Troisième texte."),
        ]
        content = ingest.build_content("veille-2026-10-02", "Corps de la veille.\n\n[^1]: note", results)
        corpus = cg.extract_corpus(content)
        assert "Premier texte" in corpus and "Troisième texte" in corpus
        assert "Ne doit pas entrer" not in corpus
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
        text = ingest.build_source_text(self.ABSTRACT, self.NARRATIVE, article_text)
        result = ingest.SourceResult(
            n=2, statut=ingest.INGEREE, detail="", titre="Meta", editeur="X", meta="X", url="u", texte=text,
        )
        content = ingest.build_content("veille-2026-10-02", self.NARRATIVE, [result])
        return cg.check_transcript([self.LINE], cg.extract_corpus(content)).violations

    def test_refused_when_only_the_abstract_was_read(self):
        assert any(v.kind == "nombre_absent" for v in self._guard(""))

    def test_accepted_once_the_full_article_extract_is_in_the_corpus(self):
        assert self._guard(self.ARTICLE_TEXT) == []
