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


class TestEnglishSources:
    """Cas réels du 2026-10-01 : sources en anglais, transcription en français."""

    def test_english_thousands_separator_matches_french_spacing(self):
        corpus = "assigned about 2,000 refugee cases; roughly 150,000 AI agents; $670,000 more"
        report = cg.check_transcript(
            ["Environ 2 000 dossiers, 150 000 agents, 670 000 dollars de plus."], corpus
        )
        assert report.violations == []

    def test_k_suffix_matches_the_full_number(self):
        report = cg.check_transcript(
            ["Un benchmark de 87 000 échantillons."], "a benchmark comprising 87k real audio"
        )
        assert report.violations == []

    def test_english_license_justifies_the_french_term(self):
        corpus = "a vendor-neutral, MIT Licensed platform for enterprises"
        report = cg.check_transcript(["Un projet sous licence MIT."], corpus)
        assert report.violations == []

    def test_a_different_number_is_still_a_violation(self):
        report = cg.check_transcript(["Environ 3 000 dossiers."], "about 2,000 refugee cases")
        assert ("nombre_absent", "3000 absent des sources") in kinds(report)

    def test_other_commercial_terms_stay_forbidden_when_only_license_is_sourced(self):
        report = cg.check_transcript(
            ["Ils vendent un abonnement annuel."], "an MIT Licensed platform"
        )
        assert ("terme_commercial", "« abonnement » absent des sources") in kinds(report)


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


class TestLength:
    def test_transcript_in_range_is_accepted(self):
        lines = ["mot " * 100] * 15
        assert cg.check_transcript(lines, "mot", word_range=(1100, 2000)).violations == []

    def test_too_long_transcript_is_a_violation(self):
        lines = ["mot " * 100] * 43
        report = cg.check_transcript(lines, "mot", word_range=(1100, 2000))
        assert [v.kind for v in report.violations] == ["duree_hors_cible"]
        assert "4300 mots" in report.violations[0].detail

    def test_too_short_transcript_is_a_violation(self):
        report = cg.check_transcript(["mot " * 50], "mot", word_range=(1100, 2000))
        assert [v.kind for v in report.violations] == ["duree_hors_cible"]

    def test_length_is_not_checked_without_a_range(self):
        assert cg.check_transcript(["mot " * 5000], "mot").violations == []

    @pytest.mark.asyncio
    async def test_node_refuses_an_over_long_transcript_before_any_voice(self, tmp_path):
        content = f"veille\n{cg.SOURCES_DELIMITER}\nmot"
        state = {
            "briefing": cg.MARKER,
            "content": content,
            "transcript": [SimpleNamespace(dialogue="mot " * 100) for _ in range(43)],
            "output_dir": tmp_path,
            "speaker_profile": SimpleNamespace(speakers=[]),
        }

        with pytest.raises(ValueError, match="aucune voix générée"):
            await cg.content_guard_node(state)


class TestScales:
    CORPUS = "funding of up to 150,000 USD, equivalent to about 3 billion VND; 2 million users"

    def test_billion_in_english_matches_milliards_in_french(self):
        for line in ("Plafonné à 3 milliards de dongs.", "Plafonné à trois milliards de dongs."):
            assert cg.check_transcript([line], self.CORPUS).violations == [], line

    def test_million_matches_digits_and_words(self):
        for line in ("Deux millions d'utilisateurs.", "2 millions d'utilisateurs."):
            assert cg.check_transcript([line], self.CORPUS).violations == [], line

    def test_a_scale_word_after_a_digit_is_not_counted_twice(self):
        assert cg.french_numbers("Plafonné à 3 milliards, soit 5 pour cent.") == []

    def test_a_number_after_pour_is_still_read(self):
        assert cg.french_numbers("pour deux mille dossiers") == [(2000, False)]

    def test_another_scale_value_is_still_a_violation(self):
        report = cg.check_transcript(["Plafonné à cinq milliards de dongs."], self.CORPUS)
        assert any(v.kind == "nombre_absent" for v in report.violations)


class TestSpelledNumbers:
    def test_parses_french_number_words(self):
        assert cg.french_numbers("quatre mille agents") == [(4000, False)]
        assert cg.french_numbers("cent cinquante mille dollars") == [(150000, False)]
        assert cg.french_numbers("quatre-vingt-seize") == [(96, False)]
        assert cg.french_numbers("deux mille vingt-six") == [(2026, False)]
        assert cg.french_numbers("quatre-vingt-sept mille échantillons") == [(87000, False)]
        assert cg.french_numbers("soixante et onze et vingt et un") == [(71, False), (21, False)]
        assert cg.french_numbers("vingt pour cent") == [(20, True)]

    def test_quatre_vingt_after_a_hundred_is_eighty_not_twenty_four(self):
        # Régression du 02/10 : « trois cent quatre-vingt mille » (380 000) était lu 324 000.
        assert cg.french_numbers("trois cent quatre-vingt mille") == [(380000, False)]
        assert cg.french_numbers("cent quatre-vingt-un") == [(181, False)]
        assert cg.french_numbers("deux cent quatre-vingts") == [(280, False)]
        assert cg.french_numbers("vingt, puis quatre") == [(20, False), (4, False)]

    def test_spelled_number_present_in_the_sources_is_accepted(self):
        corpus = "found more than 4,000 agents; about 2,000 cases; 87k audio samples"
        lines = ["Plus de quatre mille agents.", "Environ deux mille dossiers.",
                 "Quatre-vingt-sept mille échantillons."]
        assert cg.check_transcript(lines, corpus).violations == []

    def test_spelled_number_absent_from_the_sources_is_a_violation(self):
        report = cg.check_transcript(["Plus de quatre mille agents."], "about 2,000 cases")
        assert ("nombre_absent", "4000 (écrit en lettres) absent des sources") in kinds(report)

    def test_spelled_percentage_is_checked(self):
        report = cg.check_transcript(["Vingt pour cent des agents."], "13 % of organizations")
        assert any(v.kind == "nombre_absent" for v in report.violations)
        assert cg.check_transcript(["Vingt pour cent des agents."], "20 % of agents").violations == []

    def test_small_conversational_numbers_are_only_a_warning(self):
        report = cg.check_transcript(["Prenez dix minutes aujourd'hui."], "texte")
        assert report.violations == []
        assert [w.kind for w in report.warnings] == ["nombre_en_lettres_absent"]

    def test_spelled_year_matches_the_allowed_date(self):
        report = cg.check_transcript(
            ["Nous sommes en deux mille vingt-six."], "texte", extra_allowed="Date de la veille : 30 septembre 2026"
        )
        assert report.violations == []


class TestSpeakerShare:
    NAMES = ["Rim", "Khalid", "Tariq"]

    def run(self, speakers):
        return cg.check_transcript(
            ["texte"] * len(speakers), "texte", speaker_names=self.NAMES,
            speakers=speakers, min_share=0.22,
        )

    def test_balanced_conversation_is_accepted(self):
        assert self.run(["Rim", "Khalid", "Tariq", "Khalid"] * 5).violations == []

    def test_a_speaker_under_the_minimum_share_is_refused(self):
        speakers = ["Rim"] * 33 + ["Khalid"] * 31 + ["Tariq"] * 16
        report = self.run(speakers)
        assert [v.kind for v in report.violations] == ["part_intervenant"]
        assert report.violations[0].detail.startswith("Tariq ne dit que 16 réplique(s) sur 80 (20%)")

    def test_the_share_check_is_off_without_a_minimum(self):
        report = cg.check_transcript(
            ["texte"] * 3, "texte", speaker_names=self.NAMES, speakers=["Rim", "Rim", "Rim"]
        )
        assert report.violations == []


class TestClosing:
    def run(self, speakers):
        return cg.check_transcript(
            ["texte"] * len(speakers), "texte", speakers=speakers, closing_speaker="Rim"
        )

    def test_closing_by_the_host_is_accepted(self):
        assert self.run(["Rim", "Khalid", "Tariq", "Rim"]).violations == []

    def test_ending_on_another_speaker_is_refused(self):
        report = self.run(["Rim", "Khalid", "Rim", "Tariq"])
        assert [v.kind for v in report.violations] == ["cloture_absente"]
        assert report.violations[0].line == 3
        assert "elle est de Tariq" in report.violations[0].detail

    def test_the_closing_check_is_off_without_a_closing_speaker(self):
        report = cg.check_transcript(["texte"] * 2, "texte", speakers=["Rim", "Tariq"])
        assert report.violations == []


class TestOpening:
    NAMES = ["Rim", "Khalid", "Tariq"]

    def run(self, lines, speakers):
        return cg.check_transcript(
            lines, "texte", speaker_names=self.NAMES, speakers=speakers, intro_lines=10
        )

    def test_opening_that_presents_everyone_is_accepted(self):
        lines = [
            "Bonjour, ici Rim, et aujourd'hui une question qui dérange.",
            "Moi c'est Khalid, je vous donne les faits et les sources.",
            "Et moi Tariq, je pose les questions du terrain au Maroc.",
        ]
        assert self.run(lines, ["Rim", "Khalid", "Tariq"]).violations == []

    def test_opening_that_dives_into_the_facts_is_refused(self):
        lines = ["Un essai randomisé a été mené en Suisse.", "Les agents ignoraient leur bras."]
        report = self.run(lines, ["Rim", "Khalid"])
        assert [v.kind for v in report.violations] == ["presentation_absente"] * 3

    def test_a_speaker_named_but_silent_in_the_opening_is_refused(self):
        lines = ["Rim ici, avec Khalid et Tariq.", "Khalid, les faits ?"]
        report = self.run(lines, ["Rim", "Khalid"])
        assert [v.detail.split(" ")[0] for v in report.violations] == ["Tariq"]

    def test_the_opening_check_is_off_without_intro_lines(self):
        assert cg.check_transcript(["Direct aux faits."], "texte", speaker_names=["Rim"]).violations == []


class TestGuardNode:
    @pytest.fixture(autouse=True)
    def _wide_word_range(self, monkeypatch):
        monkeypatch.setattr(cg, "MIN_WORDS", 1)
        monkeypatch.setattr(cg, "MAX_WORDS", 100000)
        monkeypatch.setattr(cg, "INTRO_LINES", 0)
        monkeypatch.setattr(cg, "MIN_SPEAKER_SHARE", 0)
        monkeypatch.setattr(cg, "HOST_CLOSES", False)

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
