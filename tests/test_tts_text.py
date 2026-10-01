"""Tests de la normalisation du texte envoyé au moteur de voix.

Le moteur de voix de production répond HTTP 500 à toute phrase de moins de
7 caractères (« Oui. », « Non. », « Hmm. »), mesuré le 2026-10-01. Ces tests
verrouillent la correction : fusionner la phrase courte avec sa voisine.
"""

from open_notebook.podcasts.tts_text import (
    MIN_SENTENCE_CHARS,
    apply_lexicon,
    normalize_for_tts,
    pad_short,
    tts_text_variants,
)


class TestNormalizeForTts:
    def test_merges_short_opening_sentence_with_the_next(self):
        assert (
            normalize_for_tts("Oui. L'humain reste décideur.")
            == "Oui, L'humain reste décideur."
        )

    def test_merges_short_non(self):
        assert (
            normalize_for_tts("Non. Pas vraiment, il faut des preuves.")
            == "Non, Pas vraiment, il faut des preuves."
        )

    def test_keeps_sentences_long_enough(self):
        text = "Exactement. C'est bien ça."
        assert normalize_for_tts(text) == text

    def test_short_question_opening(self):
        assert normalize_for_tts("Quoi ? Vraiment ?") == "Quoi, Vraiment ?"

    def test_short_sentence_in_the_middle(self):
        assert (
            normalize_for_tts("Les agents se multiplient. Oui. Et c'est un défi.")
            == "Les agents se multiplient. Oui, Et c'est un défi."
        )

    def test_short_closing_sentence_joins_the_previous_one(self):
        assert normalize_for_tts("Très bien. Oui.") == "Très bien, oui."

    def test_chain_of_short_sentences(self):
        assert normalize_for_tts("Oui. Non. Peut-être bien.") == (
            "Oui, Non. Peut-être bien."
        )

    def test_abbreviation_is_not_merged(self):
        text = "M. Dupont est présent aujourd'hui."
        assert normalize_for_tts(text) == text

    def test_decimal_numbers_are_not_split(self):
        text = "Le score passe de 3.5 à 4.2 points. D'accord, merci."
        assert normalize_for_tts(text) == text

    def test_single_sentence_is_returned_unchanged(self):
        assert normalize_for_tts("Oui.") == "Oui."

    def test_blank_text_is_returned_unchanged(self):
        assert normalize_for_tts("   ") == "   "


class TestPadShort:
    def test_pads_a_too_short_reply(self):
        assert pad_short("Oui.") == "Ah oui."

    def test_keeps_the_question_mark(self):
        assert pad_short("Quoi ?") == "Ah quoi?"

    def test_long_enough_text_is_untouched(self):
        assert pad_short("Exactement.") == "Exactement."

    def test_padded_text_reaches_the_minimum(self):
        assert len(pad_short("Oui.")) >= MIN_SENTENCE_CHARS


class TestVariants:
    def test_normalized_version_comes_first_then_the_original(self):
        text = "Oui. L'humain reste décideur."
        assert tts_text_variants(text) == [
            "Oui, L'humain reste décideur.",
            text,
        ]

    def test_lone_short_reply_ends_with_the_padded_version(self):
        assert tts_text_variants("Oui.") == ["Oui.", "Ah oui."]

    def test_untouched_text_gives_a_single_variant(self):
        assert tts_text_variants("Exactement. C'est bien ça.") == [
            "Exactement. C'est bien ça."
        ]


class TestLexicon:
    def test_arxiv_is_spelled_for_the_engine_in_any_case(self):
        for written in ("arXiv", "arxiv", "ARXIV", "ArXiv"):
            assert apply_lexicon(f"Selon la source {written}, un essai.") == (
                "Selon la source arksive, un essai."
            )

    def test_only_whole_words_are_replaced(self):
        assert apply_lexicon("Le mot arxivage reste intact.") == "Le mot arxivage reste intact."

    def test_text_without_a_known_term_is_untouched(self):
        assert apply_lexicon("Khalid présente le sujet.") == "Khalid présente le sujet."

    def test_every_variant_sent_to_the_engine_carries_the_spoken_spelling(self):
        variants = tts_text_variants("Oui. Selon arXiv, c'est établi.")
        assert variants and all("arksive" in v and "arXiv" not in v for v in variants)
