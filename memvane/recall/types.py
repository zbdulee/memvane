"""Recall result limits and fixed diagnostic errors."""
from __future__ import annotations


class VectorUnavailable(Exception):
    """No query vector for this prompt; serve the published lexical lists."""
    no_query_vector = True

MAX_ROWS = 5
HYBRID_LIST_LIMIT = 20
RRF_K = 60
ROW_KEYS = frozenset({"scope", "generation", "path", "hash", "body", "score"})


class HostResponseError(ValueError):
    """Fixed diagnostic code; never includes document body or source path."""

KO_STOPWORDS = {
    "해줘", "해주세요", "해봐", "알려줘", "보여줘", "찾아줘",
    "있나", "있어", "뭐야", "어때", "인가", "인지",
    "그거", "이거", "저거", "여기", "거기",
    "좀", "것", "수", "등", "및", "또는", "그리고",
    "어떻게", "어디서", "언제", "왜", "뭘", "무엇을",
    "하나요", "할까요", "하지", "할까", "하면", "하려면",
    "나는데", "있는데", "되는데", "하는데", "인데", "건데",
    "그런데", "그래서", "그러면", "그러니", "그러나",
    # 연결어미 계열. `관련해서`는 이미 있던 `관해서`·`대해서`와 같은 부류의 누락이었다
    # (활용형 추가가 아니라 **기존 범주의 완성**이므로 어간 판정 문제와 무관하다).
    # 라이브 근거: `"이 부분 관련해서 sendbird 장애 원인"`에서 이 토큰이 AND 에 남아
    # 질의를 0건으로 만들었고, DF 필터는 이 토큰을 못 뺀다 — 코퍼스 871장 중 1장에
    # 실제로 등장하기 때문이다(DF 0 이 아니다). 빼면 같은 프롬프트가 3건을 낸다.
    "때문에", "대해서", "관해서", "관련해서", "대한", "위해", "통해", "따라",
    # 단독 토큰으로 떨어진 조사. `strip_ko_suffix`가 접미로는 이미 떼는 형태들인데
    # 사용자가 띄어 쓰면("EP12 에서 …") 토큰 하나로 남아 term 예산(5개)을 먹고
    # AND 조건만 좁힌다. 접미로 떼는 것과 단독일 때 버리는 것은 같은 정책이다.
    # 활용형(먹은/보는 …)은 여기 넣지 않는다 — 어간 판정 없이 넣으면 의미어를 잃는다.
    "에서", "으로", "에게", "처럼", "부터", "까지", "에는",
}

EN_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were",
    "how", "what", "where", "when", "why", "which",
    "do", "does", "did", "can", "could", "should",
    "please", "help", "me", "about", "this", "that",
    "it", "in", "on", "to", "for", "with", "from",
    "has", "have", "been", "be", "will", "would", "not", "no",
}
