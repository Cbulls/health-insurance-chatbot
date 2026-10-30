"""질의 intent 분류 — FAQ / 보험료 / 상품 사실."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from harag.query.faq import FaqCard, match_faq

logger = logging.getLogger("harag.query.intent")

IntentLabel = Literal["common_faq", "premium_quote", "product_fact"]

_PREMIUM_RE = re.compile(
    r"(보험료|월납|납입료|견적|공시\s*보험료|"
    r"얼마\s*(야|예요|인가요|입니까|지)|"
    r"(월|매달|매월).{0,8}(얼마|비용|가격|보험료)|"
    r"(비싸|저렴|가격).{0,12}(보험|상품))",
    re.I,
)
_ENROLL_RE = re.compile(
    r"(가입\s*(방법|절차|하)|청약|어떻게\s*가입|가입하(려면|는)|"
    r"계약\s*체결|다이렉트\s*가입)",
    re.I,
)
_FAQISH_IDS = frozenset({
    "how_to_enroll", "what_affects_premium", "renewal_vs_nonrenewal",
    "indemnity_vs_fixed", "waiting_period_general",
})


@dataclass(frozen=True)
class Intent:
    label: IntentLabel
    faq: FaqCard | None = None
    source: str = "rules"  # rules | llm


class IntentLLM(Protocol):
    """JSON 라벨만 출력하는 경량 분류기."""

    def classify_label(self, query: str) -> str | None: ...


def classify_intent_rules(query: str) -> Intent:
    q = (query or "").strip()
    faq = match_faq(q)
    if _PREMIUM_RE.search(q) or (faq and faq.id == "what_affects_premium"):
        return Intent(label="premium_quote", faq=faq or match_faq("보험료"),
                      source="rules")
    if _ENROLL_RE.search(q) or (faq and faq.id == "how_to_enroll"):
        return Intent(label="common_faq",
                      faq=faq or match_faq("가입 방법"),
                      source="rules")
    if faq and faq.id in _FAQISH_IDS:
        return Intent(label="common_faq", faq=faq, source="rules")
    return Intent(label="product_fact", faq=faq, source="rules")


def classify_intent(
    query: str,
    llm: IntentLLM | None = None,
) -> Intent:
    """1차 규칙, 애매하면 선택적 LLM JSON 라벨."""
    base = classify_intent_rules(query)
    if base.label != "product_fact" or llm is None:
        return base
    # product_fact인데 FAQ 카드가 있으면 규칙 유지
    if base.faq is not None:
        return Intent(label="common_faq", faq=base.faq, source="rules")
    try:
        raw = llm.classify_label(query)
    except Exception:  # noqa: BLE001
        logger.info("intent LLM failed; using rules", exc_info=True)
        return base
    label = _parse_label(raw)
    if label is None:
        return base
    faq = match_faq(query)
    if label == "premium_quote":
        return Intent(label="premium_quote",
                      faq=faq or match_faq("보험료"), source="llm")
    if label == "common_faq":
        return Intent(label="common_faq", faq=faq, source="llm")
    return Intent(label="product_fact", faq=faq, source="llm")


def _parse_label(raw: str | None) -> IntentLabel | None:
    if not raw:
        return None
    text = raw.strip()
    try:
        if "{" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            obj = json.loads(text[start:end])
            text = str(obj.get("intent") or obj.get("label") or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    text = text.strip().lower().replace('"', "").replace("'", "")
    if text in ("common_faq", "premium_quote", "product_fact"):
        return text  # type: ignore[return-value]
    if "premium" in text or "보험료" in text:
        return "premium_quote"
    if "faq" in text or "가입" in text:
        return "common_faq"
    if "product" in text:
        return "product_fact"
    return None


class TransportIntentLLM:
    """OpenAIChatTransport 호환 — JSON intent만."""

    def __init__(self, transport, model: str):
        self._transport = transport
        self._model = model

    def classify_label(self, query: str) -> str | None:
        prompt = (
            "다음 한국어 보험 질의를 하나의 라벨로만 분류하라. "
            "라벨: common_faq | premium_quote | product_fact\n"
            "JSON만 출력: {\"intent\":\"...\"}\n"
            "금액·병명·상품명을 새로 만들지 마라.\n"
            f"질의: {query[:500]}"
        )
        out = self._transport.post({
            "model": self._model,
            "prompt": prompt,
            "context_ids": [],
        })
        return (out.get("answer") or "").strip() or None
