"""Classify only registered expressions; all other text requires confirmation."""

HELP_NEEDED = "도움 필요"
HELP_NOT_NEEDED = "도움 불필요"
NEEDS_CONFIRMATION = "확인 필요"

REGISTERED_EXPRESSIONS = {
    "도와주세요": HELP_NEEDED,
    "사람 좀 불러주세요": HELP_NEEDED,
    "괜찮아요": HELP_NOT_NEEDED,
    "도움이 필요 없어요": HELP_NOT_NEEDED,
}


def normalize_expression(text):
    # Preserve internal punctuation: separate clauses must not become one phrase.
    return "".join(text.split()).rstrip(".!?。！？…")


def classify_response(text):
    normalized = normalize_expression(text)
    for expression, label in REGISTERED_EXPRESSIONS.items():
        if normalized == normalize_expression(expression):
            return label
    return NEEDS_CONFIRMATION
