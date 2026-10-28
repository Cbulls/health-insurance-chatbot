"""설명서 종목·문서역할·비용 성격 — 닫힌 코드만 저장한다."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("harag.brochure")

_CATALOG_PATH = Path(__file__).with_name("brochure_catalog.yaml")

PRODUCT_LINES = {
    "indemnity": "실손의료",
    "health": "건강보험",
    "cancer": "암",
    "accident": "상해",
    "dental": "치아",
    "other": "기타",
}
DOC_KINDS = {
    "summary": "상품요약서",
    "terms": "약관",
    "guide": "안내장",
}
COST_NATURES = {
    "indemnity": "실손(실제 의료비 비례)",
    "lump_sum": "정액(약정 금액)",
    "mixed": "혼합(정액+실손 특약)",
}
COVERAGE_CODES = frozenset({"outpatient", "inpatient", "tests", "injury"})
FILLED_BY = frozenset({"catalog", "llm"})
_PREMIUM_KEYS = frozenset({
    "premium", "monthly", "price", "cost", "보험료", "월납", "납입",
})


def load_brochure_catalog() -> dict:
    if not _CATALOG_PATH.is_file():
        return {}
    try:
        raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.info("brochure catalog unreadable")
        return {}
    return raw if isinstance(raw, dict) else {}


def catalog_entry(filename: str) -> dict | None:
    name = Path(filename or "").name
    rec = load_brochure_catalog().get(name)
    return rec if isinstance(rec, dict) else None


def sanitize_brochure_payload(raw: dict | None, filename: str = "") -> dict:
    """허용 코드만. 보험료·월납 필드는 버린다."""
    src = raw if isinstance(raw, dict) else {}
    cleaned = {k: v for k, v in src.items() if k not in _PREMIUM_KEYS}
    stem = Path(filename or "").stem or filename or ""
    name = str(cleaned.get("display_name") or "").strip()[:80] or stem
    line = cleaned.get("product_line")
    if line not in PRODUCT_LINES:
        line = "other"
    kind = cleaned.get("doc_kind")
    if kind not in DOC_KINDS:
        kind = ""
    cost = cleaned.get("cost_nature")
    if cost not in COST_NATURES:
        cost = ""
    summary = str(cleaned.get("summary") or "").strip()[:400]
    insurer = str(cleaned.get("insurer") or "").strip()[:40]
    source_url = _safe_http_url(cleaned.get("source_url"))
    if cleaned.get("filled_by") == "llm":
        source_url = ""
    codes = []
    seen: set[str] = set()
    for code in cleaned.get("coverage_codes") or []:
        if code in COVERAGE_CODES and code not in seen:
            seen.add(code)
            codes.append(code)
    filled = cleaned.get("filled_by")
    if filled not in FILLED_BY:
        filled = "catalog"
    return {
        "display_name": name,
        "product_line": line,
        "doc_kind": kind,
        "cost_nature": cost,
        "summary": summary,
        "insurer": insurer,
        "source_url": source_url,
        "coverage_codes": codes,
        "filled_by": filled,
    }


def labels_for(profile: dict | None) -> dict:
    p = profile or {}
    line = p.get("product_line") or ""
    kind = p.get("doc_kind") or ""
    cost = p.get("cost_nature") or ""
    return {
        "display_name": p.get("display_name") or "",
        "product_line": line,
        "product_line_label": PRODUCT_LINES.get(line, ""),
        "doc_kind": kind,
        "doc_kind_label": DOC_KINDS.get(kind, ""),
        "cost_nature": cost,
        "cost_nature_label": COST_NATURES.get(cost, ""),
        "summary": p.get("summary") or "",
        "insurer": p.get("insurer") or "",
        "source_url": p.get("source_url") or "",
        "coverage_codes": list(p.get("coverage_codes") or []),
    }


def _parse_json_object(text: str) -> dict:
    blob = (text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = blob.find("{"), blob.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(blob[start:end + 1])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


_EXTRACT_Q = (
    "설명서 앞부분만 보고 JSON 하나만 답하라. "
    "키: display_name, insurer, product_line, doc_kind, cost_nature, summary, coverage_codes. "
    "product_line은 indemnity|health|cancer|accident|dental|other 중 하나. "
    "doc_kind는 summary|terms|guide 중 하나. "
    "cost_nature는 indemnity|lump_sum|mixed 중 하나. "
    "coverage_codes는 outpatient,inpatient,tests,injury 중 해당만. "
    "insurer는 문서에 있는 보험사 이름만. URL은 쓰지 마라. "
    "보험료·월 납입·가격 숫자는 쓰지 마라. 가입을 권유하지 마라."
)


def _safe_http_url(raw) -> str:
    url = str(raw or "").strip()
    if url.startswith("https://") or url.startswith("http://"):
        return url[:512]
    return ""


def extract_brochure_with_llm(llm, preview: str, filename: str) -> dict:
    """실패하면 파일명 + other. 호출측에서 예외를 삼킬 수 있다."""
    fallback = sanitize_brochure_payload(
        {"product_line": "other", "filled_by": "llm"}, filename)
    if llm is None or not hasattr(llm, "complete"):
        return fallback
    ctx = [" ".join((preview or "").split())[:2500]]
    if not ctx[0]:
        return fallback
    try:
        answer, _ = llm.complete(_EXTRACT_Q, ctx, ["brochure-1"])
    except Exception:  # noqa: BLE001
        logger.info("brochure llm extract failed")
        return fallback
    parsed = _parse_json_object(answer or "")
    parsed["filled_by"] = "llm"
    return sanitize_brochure_payload(parsed, filename)


def upsert_catalog_profile(meta, document_id: str, owner: str,
                           filename: str) -> bool:
    entry = catalog_entry(filename)
    if entry is None or not hasattr(meta, "upsert_brochure_profile"):
        return False
    payload = sanitize_brochure_payload({**entry, "filled_by": "catalog"}, filename)
    meta.upsert_brochure_profile(document_id, owner, payload)
    return True


def upsert_extracted_profile(meta, document_id: str, owner: str,
                             filename: str, preview: str, llm=None) -> None:
    if not hasattr(meta, "upsert_brochure_profile"):
        return
    payload = extract_brochure_with_llm(llm, preview, filename)
    meta.upsert_brochure_profile(document_id, owner, payload)
