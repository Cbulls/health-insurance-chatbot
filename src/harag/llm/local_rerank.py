"""
로컬 폴백 어댑터 — 키 없이도 리랭커·멀티턴 재작성이 '실제로' 동작하게.

원칙(MVP): 관리형 GPU cross-encoder나 LLM 키가 없어도 파이프라인이 실제
재순위·재작성을 수행한다. 키가 주어지면 운영 어댑터로 교체한다.

  - LexicalCrossEncoder: 질의-청크 어절 겹침 점수(CrossEncoder Protocol 구현).
    reranker 서버 URL이 생기면 HTTP cross-encoder 어댑터로 교체.
  - IdentityRewriteLLM: 재작성 LLM 폴백. 원본 질의를 그대로 반환(무해).
  - LLMRewriteLLM: OpenAI 호환 Chat으로 지시어 해소(운영 경로).
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class LexicalCrossEncoder:
    """질의-청크 어절 겹침 기반 cross-encoder 폴백(CrossEncoder Protocol).

    점수 = |질의어 ∩ 청크어| / |질의어|  (0~1).
    dense/sparse 독립 인코딩보다 정밀한 진짜 cross-encoder는 아니지만,
    질의-청크 쌍을 함께 보고 재순위한다는 리랭커의 계약은 실제로 수행한다.
    """

    def score_pairs(self, query: str, texts: list[str]) -> list[float]:
        q = _tokens(query)
        if not q:
            return [0.0 for _ in texts]
        return [len(q & _tokens(t)) / len(q) for t in texts]


class IdentityRewriteLLM:
    """RewriteLLM 폴백 — LLM 키 없을 때. 원본 질의 그대로(재작성 생략, 무해)."""

    def rewrite(self, query: str, history: list[str]) -> str:
        return query


class LLMRewriteLLM:
    """운영 재작성 LLM — OpenAI 호환 Chat으로 지시어 해소.

    transport는 {model, prompt} → {"answer": ...} 매핑(OpenAIChatTransport 재사용).
    실패는 QueryRewriter가 잡아 원본으로 폴백(graceful degradation)한다.
    """

    def __init__(self, transport, model: str):
        self._transport = transport
        self._model = model

    def rewrite(self, query: str, history: list[str]) -> str:
        hist = "\n".join(f"- {h}" for h in history)
        prompt = (
            "다음 대화의 '이전 질의 이력'을 참고해 마지막 '후속 질의'를 "
            "지시어(그건/그거/거기 등) 없이 독립적으로 검색 가능한 한국어 질의 "
            "한 문장으로 재작성하라. 설명 없이 재작성된 질의만 출력한다.\n\n"
            f"[이전 질의 이력]\n{hist}\n\n[후속 질의]\n{query}\n\n[재작성된 질의]"
        )
        resp = self._transport.post({"model": self._model, "prompt": prompt})
        answer = (resp.get("answer") or "").strip()
        return answer or query
