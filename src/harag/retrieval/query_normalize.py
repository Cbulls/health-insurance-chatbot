"""
단일턴 질의 정규화·동의어 확장 (경량, LLM 없음).

멀티턴 rewrite와 별도로, 짧은 보험 용어 질의를 검색에 유리하게 펼친다.
"""
from __future__ import annotations

import re

# (패턴, 확장에 붙일 동의어들) — 원문 질의는 유지하고 동의어를 덧붙인다.
_SYNONYMS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"본인부담|자기부담|copay", re.I), "본인부담금 copayment"),
    (re.compile(r"보험료|프리미엄|premium", re.I), "보험료 premium"),
    (re.compile(r"요양급여|급여항목", re.I), "요양급여 급여"),
    (re.compile(r"치과|dental", re.I), "치과 dental"),
    (re.compile(r"입원|inpatient", re.I), "입원 inpatient"),
    (re.compile(r"통원|외래|outpatient", re.I), "통원 외래 outpatient"),
    (re.compile(r"면책|제외|exclusion", re.I), "면책 제외 exclusion"),
    (re.compile(r"청구|클레임|claim", re.I), "보험금 청구 claim"),
]


def expand_query_for_search(query: str) -> str:
    """원문 + 매칭된 동의어 토큰을 덧붙인 검색용 질의."""
    q = (query or "").strip()
    if not q:
        return q
    extras: list[str] = []
    for pat, syn in _SYNONYMS:
        if pat.search(q):
            for tok in syn.split():
                if tok.lower() not in q.lower() and tok not in extras:
                    extras.append(tok)
    if not extras:
        return q
    return q + " " + " ".join(extras)
