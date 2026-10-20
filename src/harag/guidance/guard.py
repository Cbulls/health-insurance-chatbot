"""출력 가드 — 진단 단정·규칙 이탈·PII 재노출을 차단하고 면책을 붙인다."""
from __future__ import annotations

import re

from harag.security.pii import PiiMasker

DISCLAIMER = (
    "이 안내는 의료 진단이 아니며, 특정 보험 상품의 모집·권유가 아닙니다. "
    "증상과 보장의 연결은 가능한 관련 영역일 뿐 확진이 아닙니다. "
    "가입·청구 전에는 약관과 전문가 상담을 확인하세요."
)

_DIAGNOSIS = re.compile(
    r"(확진|진단됩니다|진단이 됩니다|틀림없"
    r"|앓고 있|병입니다|질환입니다)")
_COMPANY_PRODUCT = re.compile(
    r"(삼성생명|삼성화재|현대해상|DB손해|KB손해|한화생명|교보생명)\s*\S*보험")


def known_coverage_labels(kb: dict | None = None) -> list[str]:
    """KB에 있는 보장 유형 라벨(허용 집합의 모집단)."""
    if kb is None:
        from harag.guidance.lexicon import load_kb
        kb = load_kb()
    return [
        spec["label"]
        for spec in (kb.get("coverages") or {}).values()
        if spec.get("label")
    ]


def guard_text(text: str, allowed_coverage_labels: list[str],
               masker: PiiMasker | None = None,
               catalog_labels: list[str] | None = None) -> tuple[str, list[str]]:
    """(정제된 텍스트, 차단 사유 목록).

    규칙 이탈은 카탈로그 라벨이 이번 요청의 허용 집합 밖에 있을 때만 자른다.
    """
    flags: list[str] = []
    out = text or ""
    if _DIAGNOSIS.search(out):
        flags.append("diagnosis_blocked")
        out = _DIAGNOSIS.sub("[진단 단정 삭제]", out)
    if _COMPANY_PRODUCT.search(out):
        flags.append("product_name_blocked")
        out = _COMPANY_PRODUCT.sub("[특정 상품명 삭제]", out)

    allowed = set(allowed_coverage_labels)
    catalog = list(catalog_labels) if catalog_labels is not None else known_coverage_labels()
    stripped = False
    for label in sorted(catalog, key=len, reverse=True):
        if not label or label not in out:
            continue
        if label not in allowed:
            stripped = True
            out = out.replace(label, "[규칙 밖 보장 삭제]")
    if stripped:
        flags.append("rule_deviation")

    masker = masker or PiiMasker()
    masked, report = masker.mask(out)
    if report:
        flags.append("pii_redacted")
        out = masked

    if DISCLAIMER not in out:
        out = out.rstrip() + "\n\n" + DISCLAIMER
    return out, flags


def split_disclaimer(text: str) -> tuple[str, str]:
    """설명 본문과 면책을 분리한다. 면책이 없으면 강제한다."""
    out = (text or "").strip()
    if DISCLAIMER in out:
        body = out.replace(DISCLAIMER, "").strip()
        return body, DISCLAIMER
    return out, DISCLAIMER


def allowed_labels(coverages) -> list[str]:
    return [c.label for c in coverages]
