"""Classify normalized whole expressions; unknown text requires confirmation."""

HELP_NEEDED = "도움 필요"
HELP_NOT_NEEDED = "도움 불필요"
NEEDS_CONFIRMATION = "확인 필요"

# Add only explicit requests for assistance, rescue or a person to help.
# Do not add symptom descriptions alone ("못 일어나요", "아파요").
HELP_REQUEST_EXPRESSIONS = (
    '도와주세요',
    '도와줘요',
    '도와줘',
    '도와주십시오',
    '좀 도와주세요',
    '저 좀 도와주세요',
    '저를 도와주세요',
    '나 좀 도와주세요',
    '나를 도와주세요',
    '저 좀 도와줘요',
    '나 좀 도와줘요',
    '제발 도와주세요',
    '제발 좀 도와주세요',
    '제발 도와줘요',
    '제발 도와줘',
    '지금 도와주세요',
    '빨리 도와주세요',
    '급히 도와주세요',
    '얼른 도와주세요',
    '여기 좀 도와주세요',
    '여기 도와주세요',
    '누가 좀 도와주세요',
    '누가 도와주세요',
    '도와주실 수 있나요',
    '저 좀 도와주실 수 있나요',
    '도와주실 수 있을까요',
    '좀 도와주시겠어요',
    '도와주셨으면 좋겠어요',
    '도움이 필요해요',
    '도움이 필요합니다',
    '도움이 필요해',
    '도움이 필요하다고요',
    '저 도움이 필요해요',
    '저는 도움이 필요해요',
    '저는 도움이 필요합니다',
    '지금 도움이 필요해요',
    '당장 도움이 필요해요',
    '정말 도움이 필요해요',
    '도움 좀 주세요',
    '저에게 도움 좀 주세요',
    '도움 좀 부탁드려요',
    '도움을 부탁드립니다',
    '도움을 요청합니다',
    '도움 요청해요',
    '사람 좀 불러주세요',
    '사람 불러주세요',
    '사람 좀 불러줘요',
    '누구 좀 불러주세요',
    '누구든 좀 불러주세요',
    '누군가 좀 불러주세요',
    '누군가 불러주세요',
    '도와줄 사람 불러주세요',
    '도와줄 사람 좀 불러주세요',
    '저를 도와줄 사람 좀 불러주세요',
    '도움 줄 사람 좀 불러주세요',
    '도와줄 분 좀 불러주세요',
    '저를 도와줄 분 좀 불러주세요',
    '사람을 불러서 도와주세요',
    '일으켜 주세요',
    '저 좀 일으켜 주세요',
    '저를 일으켜 주세요',
    '나 좀 일으켜 주세요',
    '일으켜줘요',
    '일어나는 것 좀 도와주세요',
    '일어나는 걸 도와주세요',
    '일어나게 도와주세요',
    '일어나도록 도와주세요',
    '저 좀 일어나게 도와주세요',
    '저 좀 일어날 수 있게 도와주세요',
    '일어설 수 있게 도와주세요',
    '몸 좀 일으켜 주세요',
    '혼자 못 일어나니까 도와주세요',
    '살려주세요',
    '살려줘요',
    '살려줘',
    '제발 살려주세요',
    '저 좀 살려주세요',
    '나 좀 살려주세요',
    '구조해 주세요',
    '저를 구조해 주세요',
    '구조 요청합니다',
    '구조 좀 해주세요',
    '119 불러주세요',
    '119에 신고해 주세요',
    '119에 전화해 주세요',
    '119 신고 부탁드려요',
    '구급차 불러주세요',
    '구급차 좀 불러주세요',
    '구급차 불러줘요',
    '응급차 불러주세요',
    '구급대 좀 불러주세요',
)

# Register explicit refusals. Bare or conditional replies require confirmation.
HELP_REFUSAL_EXPRESSIONS = (
    '괜찮아요',
    '괜찮습니다',
    '괜찮아',
    '저는 괜찮아요',
    '저는 괜찮습니다',
    '저 괜찮아요',
    '저 괜찮습니다',
    '전 괜찮아요',
    '전 괜찮습니다',
    '나 괜찮아요',
    '난 괜찮아요',
    '나 괜찮아',
    '난 괜찮아',
    '지금은 괜찮아요',
    '지금은 괜찮습니다',
    '이제 괜찮아요',
    '이제 괜찮습니다',
    '도움이 필요 없어요',
    '도움이 필요 없습니다',
    '도움이 필요 없다고요',
    '도움이 필요하지 않아요',
    '도움이 필요하지 않습니다',
    '도움은 필요 없어요',
    '도움은 필요 없습니다',
    '도움 필요 없어요',
    '도움 필요 없습니다',
    '저는 도움이 필요 없어요',
    '저는 도움이 필요 없습니다',
    '저는 도움이 필요하지 않아요',
    '저는 도움이 필요하지 않습니다',
    '저 도움 필요 없어요',
    '저 도움 필요 없습니다',
    '지금은 도움이 필요 없어요',
    '지금은 도움이 필요 없습니다',
    '저는 도움을 원하지 않아요',
    '도움을 원하지 않아요',
    '도움은 원하지 않아요',
    '도움은 사양할게요',
    '도움은 사양하겠습니다',
    '도와주지 않아도 돼요',
    '도와주지 않아도 됩니다',
    '도와주지 않으셔도 돼요',
    '도와주지 않으셔도 됩니다',
    '안 도와주셔도 돼요',
    '안 도와주셔도 됩니다',
    '저 안 도와주셔도 돼요',
    '도와줄 필요 없어요',
    '도와줄 필요 없습니다',
    '도와주실 필요 없어요',
    '도와주실 필요 없습니다',
    '도와주실 필요는 없어요',
    '도와주실 필요는 없습니다',
    '저를 도와주실 필요 없어요',
    '도와주지 마세요',
    '저를 도와주지 마세요',
    '도움을 받지 않아도 돼요',
    '도움을 받지 않아도 됩니다',
    '저는 도움을 받지 않아도 돼요',
    '저는 도움을 받지 않아도 됩니다',
    '도움 안 받아도 돼요',
    '도움 안 받아도 됩니다',
)

REGISTERED_EXPRESSIONS = {}


def normalize_expression(text):
    # Preserve internal punctuation: separate clauses must not become one phrase.
    # Legacy behavior: whitespace and terminal punctuation are ignored.
    return "".join(text.split()).rstrip(".!?。！？…")


# Check both groups before building the normalized lookup.
_NORMALIZED_EXPRESSIONS = {}
for label, expressions in (
    (HELP_NEEDED, HELP_REQUEST_EXPRESSIONS),
    (HELP_NOT_NEEDED, HELP_REFUSAL_EXPRESSIONS),
):
    for expression in expressions:
        key = normalize_expression(expression)
        if not key or key in _NORMALIZED_EXPRESSIONS:
            raise ValueError(f"Duplicate/empty normalized response expression: {expression!r}")
        REGISTERED_EXPRESSIONS[expression] = label
        _NORMALIZED_EXPRESSIONS[key] = label


def classify_response(text):
    return _NORMALIZED_EXPRESSIONS.get(normalize_expression(text), NEEDS_CONFIRMATION)
