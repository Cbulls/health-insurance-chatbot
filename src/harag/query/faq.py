"""닫힌 FAQ 카드 — 가입·보험료·용어 등 설명서 밖 공통 안내."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_FAQ_PATH = Path(__file__).resolve().parent.parent / "guidance" / "faq.yaml"


@dataclass(frozen=True)
class FaqCard:
    id: str
    triggers: tuple[str, ...]
    blurb: str
    followups: tuple[str, ...]
    never_claim: tuple[str, ...]


@lru_cache(maxsize=1)
def load_faq() -> tuple[FaqCard, ...]:
    if not _FAQ_PATH.is_file():
        return ()
    try:
        raw = json.loads(_FAQ_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    cards: list[FaqCard] = []
    for item in (raw.get("cards") or []) if isinstance(raw, dict) else []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        blurb = str(item.get("blurb") or "").strip()
        if not cid or not blurb:
            continue
        triggers = tuple(
            str(t).strip() for t in (item.get("triggers") or []) if str(t).strip()
        )
        followups = tuple(
            str(t).strip() for t in (item.get("followups") or []) if str(t).strip()
        )
        never = tuple(
            str(t).strip() for t in (item.get("never_claim") or []) if str(t).strip()
        )
        cards.append(FaqCard(
            id=cid, triggers=triggers, blurb=blurb,
            followups=followups, never_claim=never,
        ))
    return tuple(cards)


def match_faq(query: str, cards: tuple[FaqCard, ...] | None = None) -> FaqCard | None:
    """가장 긴 trigger가 질의에 포함된 카드. 없으면 None."""
    hay = (query or "").strip()
    if not hay:
        return None
    cards = cards if cards is not None else load_faq()
    best: FaqCard | None = None
    best_len = 0
    for card in cards:
        for trig in card.triggers:
            if trig and trig in hay and len(trig) > best_len:
                best = card
                best_len = len(trig)
    return best


def format_faq_answer(card: FaqCard, *, extra: str = "") -> str:
    parts = [card.blurb.strip()]
    if extra.strip():
        parts.append(extra.strip())
    if card.followups:
        parts.append(
            "다음에 이렇게 물어보시면 더 정확히 찾을 수 있습니다.\n"
            + "\n".join(f"· {q}" for q in card.followups[:3])
        )
    return "\n\n".join(parts)


def premium_template_answer(
    *,
    display_name: str = "",
    insurer: str = "",
    summary: str = "",
    cost_nature: str = "",
    source_url: str = "",
) -> str:
    """개인 월납 확정 금지 + 확인 경로 안내."""
    head = (
        "개인에게 적용되는 월 보험료는 이 채팅만으로 확정할 수 없습니다. "
        "나이·성별·선택 담보·가입금액·고지형 등에 따라 달라지며, "
        "상품요약서에 공개 예시가 없거나 찾기 어려운 경우도 많습니다."
    )
    bits: list[str] = []
    if display_name or insurer:
        label = display_name or "선택 상품"
        if insurer:
            label = f"{label} ({insurer})"
        bits.append(f"선택 상품: {label}")
    if cost_nature:
        bits.append(f"비용 성격(카탈로그): {cost_nature}")
    if summary:
        bits.append(f"상품 요약: {summary[:400]}")
    mid = "\n".join(bits)
    tail_lines = [
        "확인 방법:",
        "· 보험사 가격공시·상품 공시 자료",
        "· 공식 홈페이지·고객센터·설계사 상담",
    ]
    if source_url:
        tail_lines.append(f"· 공식 안내 링크: {source_url}")
    tail = "\n".join(tail_lines)
    parts = [head]
    if mid:
        parts.append(mid)
    parts.append(tail)
    return "\n\n".join(parts)
