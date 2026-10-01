"""Tests du contrôle de contenu d'un podcast de veille (content_guard.py).

La veille de l'éditeur n'est pas une preuve : la transcription est confrontée aux
textes des sources que Diwan a lui-même récupérés.
"""

import json
from types import SimpleNamespace

import pytest

from open_notebook.podcasts import content_guard as cg

SOURCES = (
    "Le projet publie une licence Apache pour son code. Le gouvernement vietnamien "
    "débloque 150 000 dollars. Le score passe de 23,2 % à 32 %. Rabat accueille 12 équipes."
)


def kinds(report):
    return sorted({(v.kind, v.detail) for v in report.violations})


class TestCheckTranscript:
    def test_conform_transcript_has_no_violation(self):
        report = cg.check_transcript(
            ["Le gouvernement vietnamien débloque 150 000 dollars."], SOURCES
        )
        assert report.violations == []

    def test_invented_commercial_term_is_a_violation(self):
        report = cg.check_transcript(["Ce produit se vend en abonnement annuel."], SOURCES)
        assert ("terme_commercial", "« abonnement » absent des sources") in kinds(report)

    def test_commercial_term_present_in_sources_is_allowed(self):
        report = cg.check_transcript(["Le code est sous licence Apache."], SOURCES)
        assert report.violations == []

    def test_invented_licence_sale_is_caught_when_the_source_has_no_licence(self):
        report = cg.check_transcript(
            ["Un tel outil se vend en licence à l'administration."],
            "Le gouvernement vietnamien débloque 150 000 dollars.",
        )
        assert any(v.kind == "terme_commercial" for v in report.violations)

    def test_number_absent_from_sources_is_a_violation(self):
        report = cg.check_transcript(["Cela représente 87 % des cas."], SOURCES)
        assert ("nombre_absent", "87 absent des sources") in kinds(report)

    def test_thousand_separator_and_decimal_comma_are_normalized(self):
        report = cg.check_transcript(["150000 dollars, soit 23.2 % de plus."], SOURCES)
        assert report.violations == []

    def test_single_digit_without_unit_is_not_checked(self):
        assert cg.check_transcript(["Trois ou 2 équipes."], SOURCES).violations == []

    def test_single_digit_with_percent_is_checked(self):
        report = cg.check_transcript(["Seulement 7 % des équipes."], SOURCES)
        assert ("nombre_absent", "7 absent des sources") in kinds(report)

    def test_date_line_numbers_are_allowed(self):
        report = cg.check_transcript(
            ["Nous sommes le 30 septembre."],
            SOURCES,
            extra_allowed="Date de la veille : 30 septembre 2026",
        )
        assert report.violations == []

    def test_proper_noun_absent_is_only_a_warning(self):
        report = cg.check_transcript(["On en parle avec Zorglub aujourd'hui."], SOURCES)
        assert report.violations == []
        assert [w.kind for w in report.warnings] == ["nom_propre_absent"]

    def test_speaker_names_are_not_warned(self):
        report = cg.check_transcript(
            ["Merci beaucoup, Khalid pour ce point."], SOURCES, speaker_names=["Khalid"]
        )
        assert report.warnings == []


class TestExtractCorpus:
    def test_returns_text_after_the_delimiter(self):
        content = f"fil conducteur\n{cg.SOURCES_DELIMITER}\ntexte des sources"
        assert "texte des sources" in cg.extract_corpus(content)
        assert "fil conducteur" not in cg.extract_corpus(content)

    def test_missing_delimiter_fails_closed(self):
        with pytest.raises(ValueError, match="corpus des sources"):
            cg.extract_corpus("aucun séparateur ici")


def make_state(tmp_path, briefing, lines, content):
    return {
        "briefing": briefing,
        "content": content,
        "transcript": [SimpleNamespace(dialogue=line) for line in lines],
        "output_dir": tmp_path,
        "speaker_profile": SimpleNamespace(speakers=[SimpleNamespace(name="Rim")]),
    }


class TestGuardNode:
    @pytest.mark.asyncio
    async def test_without_marker_the_guard_is_inactive(self, tmp_path):
        state = make_state(tmp_path, "briefing sans marqueur", ["abonnement à 99 euros"], "x")

        assert await cg.content_guard_node(state) == {}
        assert not (tmp_path / "guard_report.json").exists()

    @pytest.mark.asyncio
    async def test_conform_transcript_passes_and_writes_the_report(self, tmp_path):
        content = f"veille\n{cg.SOURCES_DELIMITER}\n{SOURCES}"
        state = make_state(
            tmp_path, f"{cg.MARKER}\nbriefing", ["Rabat accueille 12 équipes."], content
        )

        assert await cg.content_guard_node(state) == {}
        report = json.loads((tmp_path / "guard_report.json").read_text(encoding="utf-8"))
        assert report["violations"] == []

    @pytest.mark.asyncio
    async def test_violation_raises_before_any_voice(self, tmp_path):
        content = f"veille avec 87 %\n{cg.SOURCES_DELIMITER}\n{SOURCES}"
        state = make_state(
            tmp_path, f"{cg.MARKER}\nbriefing", ["Cela représente 87 % des cas."], content
        )

        with pytest.raises(ValueError, match="aucune voix générée"):
            await cg.content_guard_node(state)
        report = json.loads((tmp_path / "guard_report.json").read_text(encoding="utf-8"))
        assert report["violations"][0]["type"] == "nombre_absent"

    @pytest.mark.asyncio
    async def test_marker_without_sources_fails_closed(self, tmp_path):
        state = make_state(tmp_path, cg.MARKER, ["Une réplique."], "contenu sans séparateur")

        with pytest.raises(ValueError, match="corpus des sources"):
            await cg.content_guard_node(state)
