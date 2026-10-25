"""Test exact-expression matching using only the Python standard library."""

import unittest

from response_classifier import classify_response


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
        for text in ("살려주세요", "혼자 일어날 수가 없어요", "안녕하세요", "지금 괜찮아요"):
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
