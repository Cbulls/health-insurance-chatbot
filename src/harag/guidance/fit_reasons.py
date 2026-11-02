"""상품별 적합 이유 3축(증상·걱정·조건) — 결정론 fallback + LLM·공식 페이지."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from harag.guidance.engine import CONCERNS, OCCUPATIONS, RuleResult

logger = logging.getLogger("harag.fit_reasons")

ASPECTS = ("symptom", "concern", "condition")
_ASPECT_KO = {
    "symptom": "증상",
    "concern": "걱정",
    "condition": "조건",
}
_DOC_MARKER_RE = re.compile(
    r"\[\s*문서\s*\d+(?:\s*,\s*\d+)*\s*\]",
)


def _i_ga(word: str) -> str:
    """주격 조사: 받침 있으면 '이', 없으면 '가'(한글 아니면 '이')."""
    w = (word or "").strip()
    if not w:
        return "이"
    code = ord(w[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return "이" if (code - 0xAC00) % 28 else "가"
    return "이"


def fit_reasons_fallback(
        product: dict,
        rules: RuleResult,
        *,
        age_band: str | None = None,
        sex: str | None = None,
        care: dict | None = None) -> list[dict[str, str]]:
    """웹·LLM 없이도 항상 3개."""
    name = (
        str(product.get("name") or product.get("title") or "이 요약서")
        .strip()
    )
    symptoms = [h.label for h in (rules.symptoms or []) if h.label][:4]
    symptom_bit = "·".join(symptoms) if symptoms else "선택하신 증상"
    clabs = [CONCERNS[c] for c in (rules.concerns or []) if c in CONCERNS]
    concern_bit = "·".join(clabs) if clabs else "선택하신 걱정"
    matched = [
        str(m).strip()
        for m in (product.get("matched_coverages") or [])
        if str(m).strip()
    ][:4]
    match_bit = "·".join(matched) if matched else "안내된 보장 축"
    nature = (
        str(product.get("cost_nature_label") or "").strip()
        or str(product.get("cost_nature") or "").strip()
        or "상품 성격"
    )
    summary = str(product.get("short_summary") or product.get("summary") or "")
    summary_hint = ""
    if summary:
        first = summary.split(".")[0].strip()
        if first:
            summary_hint = first[:80]

    cond_bits: list[str] = []
    occ = OCCUPATIONS.get(rules.occupation or "")
    if occ:
        cond_bits.append(occ)
    from harag.guidance.age_bands import AGE_UI_LABELS
    if age_band and age_band in AGE_UI_LABELS:
        cond_bits.append(AGE_UI_LABELS[age_band])
    sex_lab = {"male": "남성", "female": "여성"}.get(sex or "")
    if sex_lab:
        cond_bits.append(sex_lab)
    care = care or {}
    sil = care.get("existing_silson")
    if sil == "no":
        cond_bits.append("실손 미보유(응답)")
    elif sil == "yes":
        cond_bits.append("실손 보유(응답)")
    cond_bit = "·".join(cond_bits) if cond_bits else "입력하신 상황"

    symptom_text = (
        f"{symptom_bit}과 연결해 볼 수 있는 {match_bit}{_i_ga(match_bit)} "
        f"{name} 요약서에 안내되어 있어요."
    )
    if clabs:
        concern_text = (
            f"걱정 「{concern_bit}」에 맞춰 {nature} 성격의 보장을 "
            f"이 요약서에서 함께 점검할 수 있어요."
        )
        if summary_hint and "실손" in summary_hint:
            concern_text = (
                f"걱정 「{concern_bit}」과 관련해, 요약서에 "
                f"「{summary_hint}」로 안내되어 있어요."
            )
    else:
        # 걱정 미선택 — placeholder 「선택하신 걱정」 노출 방지
        concern_text = (
            f"따로 고른 걱정이 없어 {nature} 성격의 보장을 기본으로 "
            f"이 요약서에서 점검할 수 있어요."
        )
        if summary_hint and "실손" in summary_hint:
            concern_text = (
                f"요약서에 「{summary_hint}」로 안내되어 있어 "
                f"병원비 부담 관점에서 함께 볼 수 있어요."
            )
    condition_text = (
        f"{cond_bit}을 참고해 고지·특약 구성을 요약서 기준으로 "
        f"비교해 볼 수 있어요. 가입 가능 여부는 약관·상담이 우선입니다."
    )
    return [
        {"aspect": "symptom", "text": symptom_text},
        {"aspect": "concern", "text": concern_text},
        {"aspect": "condition", "text": condition_text},
    ]


def _web_snippets_for_product(
        product: dict, browser, *, query: str) -> str:
    if browser is None:
        return ""
    try:
        from harag.retrieval.official_web import resolve_allowlist_for_products
        entries = resolve_allowlist_for_products([{
            "insurer": product.get("insurer") or "",
            "source_url": product.get("source_url") or "",
            "display_name": (
                product.get("name") or product.get("title") or ""
            ),
        }])
        if not entries:
            return ""
        chunks = browser.browse(query, entries) or []
        parts: list[str] = []
        for sc in chunks[:2]:
            ch = getattr(sc, "chunk", None) or sc
            text = getattr(ch, "text", None) or getattr(ch, "content", "") or ""
            text = str(text).strip()
            if text:
                parts.append(text[:1200])
        return "\n---\n".join(parts)[:2400]
    except Exception as e:  # noqa: BLE001
        logger.info("fit_reasons web skip: %s", type(e).__name__)
        return ""


def strip_doc_markers(text: str) -> str:
    """적합 이유·카드 카피에서 [문서 N] 제거 (PDF 링크로 충분)."""
    out = _DOC_MARKER_RE.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+\.", ".", out)
    return out.strip()


def _parse_fit_reasons_json(raw: str) -> list[dict[str, str]]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            start = text.find("[")
            end = text.rfind("]") + 1
        if start < 0 or end <= start:
            return []
        obj = json.loads(text[start:end])
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    rows = obj.get("reasons") if isinstance(obj, dict) else obj
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        aspect = str(row.get("aspect") or "").strip().lower()
        if aspect in _ASPECT_KO.values():
            # 한글 aspect → 영문 키
            rev = {v: k for k, v in _ASPECT_KO.items()}
            aspect = rev.get(aspect, "")
        if aspect not in ASPECTS or aspect in seen:
            continue
        body = strip_doc_markers(str(row.get("text") or ""))
        if not body or len(body) < 8:
            continue
        # 가입 강요 제거
        if re.search(r"가입하세요|가입을 추천|가입을 권유", body):
            continue
        seen.add(aspect)
        out.append({"aspect": aspect, "text": body[:220]})
    return out


def enrich_fit_reasons_with_llm(
        product: dict,
        rules: RuleResult,
        llm,
        *,
        web_text: str = "",
        age_band: str | None = None,
        sex: str | None = None,
        care: dict | None = None) -> list[dict[str, str]] | None:
    if llm is None or not hasattr(llm, "complete"):
        return None
    fb = fit_reasons_fallback(
        product, rules, age_band=age_band, sex=sex, care=care)
    name = str(product.get("name") or product.get("title") or "").strip()
    facts = {
        "product": name,
        "insurer": product.get("insurer") or "",
        "cost_nature": product.get("cost_nature_label")
        or product.get("cost_nature") or "",
        "matched_coverages": product.get("matched_coverages") or [],
        "summary": (product.get("short_summary") or product.get("summary") or "")[:400],
        "symptoms": [h.label for h in (rules.symptoms or [])],
        "concerns": [
            CONCERNS[c] for c in (rules.concerns or []) if c in CONCERNS
        ],
        "occupation": OCCUPATIONS.get(rules.occupation or "") or "",
        "age_band": age_band or "",
        "sex": sex or "",
        "care": care or {},
        "official_page_excerpt": (web_text or "")[:2000],
        "fallback_hints": fb,
    }
    prompt = (
        "당신은 보험 상품요약서·공식 안내 페이지를 근거로, "
        "사용자 입력과의 적합 이유를 쓰는 도우미입니다. "
        "확진·가입 권유·금액 확정 금지. "
        "[문서 N]·[문서1] 같은 인용 마커는 절대 쓰지 마세요. "
        "JSON만 출력: "
        '{"reasons":[{"aspect":"symptom","text":"..."},'
        '{"aspect":"concern","text":"..."},'
        '{"aspect":"condition","text":"..."}]} '
        "aspect는 symptom(증상)·concern(걱정)·condition(직업·성별·연령 등 조건) "
        "세 개를 모두 포함하고, text는 쉬운 존댓말 1문장. "
        "official_page_excerpt와 summary·matched_coverages에 있는 사실만 연결하고 "
        "없는 보장은 만들지 마세요."
    )
    try:
        answer, _ = llm.complete(
            prompt, [json.dumps(facts, ensure_ascii=False)], ["fit-1"])
        parsed = _parse_fit_reasons_json(answer or "")
        if len(parsed) >= 3:
            # 순서 고정
            by = {r["aspect"]: r for r in parsed}
            return [by[a] for a in ASPECTS if a in by][:3]
        if parsed:
            # 부족한 aspect는 fallback으로 채움
            by = {r["aspect"]: r for r in parsed}
            for r in fb:
                by.setdefault(r["aspect"], r)
            return [by[a] for a in ASPECTS]
    except Exception as e:  # noqa: BLE001
        logger.info("fit_reasons llm skip: %s", type(e).__name__)
    return None


def attach_fit_reasons(
        products: list[dict],
        rules: RuleResult,
        *,
        llm=None,
        web_browser=None,
        age_band: str | None = None,
        sex: str | None = None,
        care: dict | None = None) -> list[dict]:
    """각 상품에 fit_reasons(최소 3) 부여. 실패해도 fallback."""
    out: list[dict] = []
    for p in products or []:
        row = dict(p)
        fb = fit_reasons_fallback(
            row, rules, age_band=age_band, sex=sex, care=care)
        reasons = fb
        if llm is not None:
            name = str(row.get("name") or row.get("title") or "").strip()
            query = (
                f"{name} 보장 특약 안내 "
                + " ".join(
                    CONCERNS[c] for c in (rules.concerns or []) if c in CONCERNS
                )
            ).strip()
            web_text = _web_snippets_for_product(
                row, web_browser, query=query)
            llm_reasons = enrich_fit_reasons_with_llm(
                row, rules, llm,
                web_text=web_text,
                age_band=age_band, sex=sex, care=care)
            if llm_reasons and len(llm_reasons) >= 3:
                reasons = llm_reasons
        cleaned: list[dict[str, str]] = []
        for r in reasons:
            text = strip_doc_markers(str(r.get("text") or ""))
            if not text:
                continue
            cleaned.append({
                "aspect": str(r.get("aspect") or ""),
                "text": text,
            })
        if len(cleaned) < 3:
            cleaned = [
                {"aspect": r["aspect"],
                 "text": strip_doc_markers(r["text"])}
                for r in fb
            ]
        row["fit_reasons"] = cleaned
        out.append(row)
    return out
