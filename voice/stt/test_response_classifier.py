"""Test exact-expression matching using only the Python standard library."""

import unittest
from pathlib import Path

from response_classifier import (
    HELP_NEEDED, HELP_NOT_NEEDED, NEEDS_CONFIRMATION,
    HELP_REQUEST_EXPRESSIONS, HELP_REFUSAL_EXPRESSIONS,
    REGISTERED_EXPRESSIONS, normalize_expression, classify_response,
)


class ResponseClassifierTests(unittest.TestCase):
    def test_registered_expressions(self):
        cases = {
            "도와주세요": "도움 필요",
            "사람 좀 불러주세요": "도움 필요",
            "괜찮아요": "도움 불필요",
            "도움이 필요 없어요": "도움 불필요",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), expected)

    def test_every_registered_expression_is_classified(self):
        # Entire list is the test matrix: no manual per-expression tests needed.
        self.assertGreaterEqual(len(HELP_REQUEST_EXPRESSIONS), 60)
        self.assertGreaterEqual(len(HELP_REFUSAL_EXPRESSIONS), 40)
        self.assertEqual(len(REGISTERED_EXPRESSIONS),
                         len(HELP_REQUEST_EXPRESSIONS) + len(HELP_REFUSAL_EXPRESSIONS))
        for label, expressions in (
            (HELP_NEEDED, HELP_REQUEST_EXPRESSIONS),
            (HELP_NOT_NEEDED, HELP_REFUSAL_EXPRESSIONS),
        ):
            for expression in expressions:
                with self.subTest(label=label, expression=expression):
                    self.assertEqual(classify_response(expression), label)
                    self.assertEqual(classify_response(f"  {expression}! "), label)
                    self.assertEqual(classify_response(" ".join(expression)), label)

    def test_no_colliding_normalized_expressions(self):
        expressions = list(REGISTERED_EXPRESSIONS)
        self.assertEqual(len(expressions), len({normalize_expression(s) for s in expressions}))

    def test_conflicting_registrations_are_rejected_before_lookup_is_built(self):
        source_path = Path(__file__).with_name("response_classifier.py")
        source = source_path.read_text(encoding="utf-8")
        for duplicate in ("도와주세요", "도와 주세요"):
            with self.subTest(duplicate=duplicate):
                conflicting = source.replace("    '괜찮습니다',", f"    {duplicate!r},", 1)
                self.assertNotEqual(conflicting, source)
                with self.assertRaisesRegex(ValueError, "Duplicate/empty"):
                    exec(compile(conflicting, str(source_path), "exec"), {})

    def test_negated_or_conflicting_extensions_are_not_partial_matches(self):
        # Adding a long allowlist must not broaden matching to substrings.
        for expression in HELP_REQUEST_EXPRESSIONS:
            for modified in (f"{expression}라고 말한 적 없어요",
                             f"{expression}. 하지만 도움은 필요 없어요"):
                with self.subTest(text=modified):
                    self.assertEqual(classify_response(modified), NEEDS_CONFIRMATION)
        for expression in HELP_REFUSAL_EXPRESSIONS:
            for modified in (f"{expression}. 그런데 도와주세요",
                             f"{expression}라고 들었어요"):
                with self.subTest(text=modified):
                    self.assertEqual(classify_response(modified), NEEDS_CONFIRMATION)

    def test_unrecognized_symptoms_and_ambiguous_refusals_need_confirmation(self):
        for text in (
            "아파요", "못 일어나요", "혼자 일어나기 힘들어요", "어지러워요",
            "도와주세요라고 하면 되나요", "도와주세요? 아니요", "도와주지 말라는 뜻이 아니에요",
            "괜찮지 않아요", "안 괜찮아요", "괜찮긴 한데 일어날 수가 없어요",
            "괜찮아요. 하지만 도움이 필요해요", "도움은 필요 없어요. 구급차 불러주세요",
            "지금은 괜찮을 것 같아요", "도움이 필요 없는 건 아니에요",
            "아니요", "네", "됐어요", "필요 없어요", "혼자 할 수 있어요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), NEEDS_CONFIRMATION)

    def test_whitespace_and_trailing_punctuation(self):
        cases = {
            "  도와 주세요!!!\n": "도움 필요",
            "사람\t좀\n불러주세요。": "도움 필요",
            "괜찮 아요…": "도움 불필요",
            " 도움이 필요없어요？！ ": "도움 불필요",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), expected)

    def test_unregistered_phrases_require_confirmation(self):
        for text in ("아파요", "혼자 일어날 수가 없어요", "안녕하세요", "갑자기 어지러워요", "지금 괜찮아요"):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), "확인 필요")

    def test_negations_are_not_matched_by_substring(self):
        for text in (
            "안 도와주세요",
            "도와달라는 뜻은 아니에요",
            "도움이 필요 없는 건 아니에요",
            "괜찮지 않아요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), "확인 필요")

    def test_mixed_and_conflicting_sentences_require_confirmation(self):
        for text in (
            "도와주세요. 그런데 도움은 필요 없어요.",
            "괜찮아요. 하지만 혼자서는 못 일어나겠어요.",
            "도움이 필요 없어요. 도와주세요.",
            "도와주세요 괜찮아요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), "확인 필요")

    def test_question_or_quoted_request_is_not_a_registered_request(self):
        for text in ("도움이 필요한지 물어보신 건가요?", "도와주세요라고 하면 되나요?"):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), "확인 필요")

    def test_empty_recognition_requires_confirmation(self):
        for text in ("", " \t\n", "...", "？！", "…"):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), "확인 필요")

    def test_internal_punctuation_is_not_removed(self):
        for text in ("도와.주세요", "괜찮.아요", "도움이 필요?없어요"):
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), "확인 필요")


if __name__ == "__main__":
    unittest.main(verbosity=2)
