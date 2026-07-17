"""
로컬 폴백 어댑터 — 키 없이도 리랭커·멀티턴 재작성이 '실제로' 동작하게.

원칙(MVP): 관리형 GPU cross-encoder나 LLM 키가 없어도 파이프라인이 실제
재순위·재작성을 수행한다. 키가 주어지면 운영 어댑터로 교체한다.

  - LexicalCrossEncoder: 질의-청크 어절 겹침 점수(CrossEncoder Protocol 구현).
    RERANKER_SERVER_URL이 있으면 HttpCrossEncoder(llm/http_rerank.py)로 교체.
  - IdentityRewriteLLM: 재작성 LLM 폴백. 원본 질의를 그대로 반환(무해).
  - LLMRewriteLLM: OpenAI 호환 Chat으로 지시어 해소(운영 경로).
"""
from __future__ import annotations

import json
import logging
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


class LLMCrossEncoder:
    """LLM 기반 pointwise 리랭커(CrossEncoder Protocol) — 운영 경로.

    질의-후보 전체를 '한 번의' Chat 호출에 배치로 넣고 0~10 관련도 점수의
    JSON 배열을 받는다(후보당 호출 아님 — 쿼터·지연 절약).
    호출·파싱 실패 시 LexicalCrossEncoder 점수로 폴백한다(검색은 계속 동작).
    """

    _MAX_CHARS_PER_DOC = 600  # 후보 절단 — 프롬프트 폭주 방지

    def __init__(self, transport, model: str):
        self._transport = transport
        self._model = model
        self._fallback = LexicalCrossEncoder()

    def score_pairs(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        try:
            scores = self._score_llm(query, texts)
            if scores is not None:
                return scores
        except Exception:  # noqa: BLE001 — 리랭커 장애가 검색을 죽이면 안 됨
            logging.getLogger("harag.rerank").warning(
                "LLM rerank failed — lexical fallback", exc_info=True)
        return self._fallback.score_pairs(query, texts)

    def _score_llm(self, query: str, texts: list[str]) -> list[float] | None:
        docs = "\n".join(
            f"[{i + 1}] {t[:self._MAX_CHARS_PER_DOC]}"
            for i, t in enumerate(texts))
        prompt = (
            "너는 검색 결과 평가자다. 질의에 대한 각 문서의 관련도를 "
            "0(무관)~10(직접 답변 근거) 정수로 평가하라.\n"
            f"문서는 {len(texts)}개다. 설명 없이 길이 {len(texts)}의 "
            "JSON 정수 배열만 출력한다. 예: [7, 0, 3]\n\n"
            f"질의: {query}\n\n{docs}"
        )
        resp = self._transport.post({"model": self._model, "prompt": prompt})
        raw = (resp.get("answer") or "").strip()
        m = re.search(r"\[[\d\s,.-]*\]", raw)
        if m is None:
            return None
        scores = json.loads(m.group())
        if not isinstance(scores, list) or len(scores) != len(texts):
            return None
        return [max(0.0, min(10.0, float(s))) / 10.0 for s in scores]


class IdentityRewriteLLM:
    """RewriteLLM 폴백 — LLM 키 없을 때. 원본 질의 그대로(재작성 생략, 무해)."""

    def rewrite(self, query: str, history: list[str]) -> str:
        return query


class LLMRewriteLLM:
    """운영 재작성 LLM — OpenAI 호환 Chat으로 지시어 해소.

    transport는 {model, prompt} → {"answer": ...} 매핑(OpenAIChatTransport 재사용).
    실패는 QueryRewriter가 잡아 원본으로 폴백(graceful degradation)한다.
    max_chars로 이력+질의 프롬프트를 잘라 Gemini 토큰·쿼터를 절약한다.
    """

    def __init__(self, transport, model: str, max_chars: int = 2000):
        self._transport = transport
        self._model = model
        self._max_chars = max(200, max_chars)

    def rewrite(self, query: str, history: list[str]) -> str:
        # 최근 이력부터 넣어 예산 안에서 최대한 맥락 유지
        lines: list[str] = []
        budget = self._max_chars - len(query) - 120
        for h in reversed(list(history)):
            piece = f"- {h}"
            if sum(len(x) + 1 for x in lines) + len(piece) > budget:
                break
            lines.append(piece)
        lines.reverse()
        hist = "\n".join(lines) if lines else "(없음)"
        prompt = (
            "다음 대화의 '이전 질의 이력'을 참고해 마지막 '후속 질의'를 "
            "지시어(그건/그거/거기 등) 없이 독립적으로 검색 가능한 한국어 질의 "
            "한 문장으로 재작성하라. 설명 없이 재작성된 질의만 출력한다.\n\n"
            f"[이전 질의 이력]\n{hist}\n\n[후속 질의]\n{query}\n\n[재작성된 질의]"
        )
        if len(prompt) > self._max_chars:
            prompt = prompt[: self._max_chars]
        resp = self._transport.post({"model": self._model, "prompt": prompt})
        answer = (resp.get("answer") or "").strip()
        return answer or query
