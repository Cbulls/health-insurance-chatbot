"""
로컬 추출형(extractive) LLM — 키 없이 도는 오프라인 폴백.

생성형 LLM이 없을 때, 검색된 컨텍스트에서 질의와 가장 겹치는 문장을 골라
'문서 기반' 답변으로 되돌려준다. 지어내지 않으므로 RAG의 정직성 원칙과 부합하며,
실제 청크 id를 인용해 생성기의 인용 검증도 통과한다. 데모/스모크 테스트용.
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_SENT = re.compile(r"[^.!?。\n]+[.!?。]?")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class LocalExtractiveLLM:
    model_id = "local-extractive"

    def complete(self, query: str, context_texts: list[str],
                 context_ids: list[str]) -> tuple[str, list[str]]:
        q = _tokens(query)
        # 질의 토큰과 가장 많이 겹치는 컨텍스트 선택
        best_i, best_overlap = 0, -1
        for i, txt in enumerate(context_texts):
            overlap = len(q & _tokens(txt))
            if overlap > best_overlap:
                best_i, best_overlap = i, overlap

        best_text = context_texts[best_i]
        snippet = self._best_sentence(best_text, q) or best_text[:300]
        answer = f"문서 근거: {snippet.strip()}"
        return answer, [context_ids[best_i]]

    @staticmethod
    def _best_sentence(text: str, q: set[str]) -> str | None:
        best, best_overlap = None, 0
        for m in _SENT.finditer(text):
            s = m.group().strip()
            if not s:
                continue
            overlap = len(q & _tokens(s))
            if overlap > best_overlap:
                best, best_overlap = s, overlap
        return best
