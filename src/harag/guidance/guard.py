"""출력 가드 — 진단 단정·규칙 이탈·PII 재노출을 차단하고 면책을 붙인다."""
from __future__ import annotations

import re

from harag.security.pii import PiiMasker

DISCLAIMER = (
    "이 안내는 의료 진단이 아니며, 특정 보험 상품의 모집·권유가 아닙니다. "
    "증상과 보장의 연결은 가능한 관련 영역일 뿐 확진이 아닙니다. "
    "가입·청구 전에는 약관과 전문가 상담을 확인하세요."
)

# 긍정 단정만 차단. 「확진이 아닙니다」 등 부정 면책은 유지한다.
_DIAGNOSIS = re.compile(
    r"(확진됩니다|확진입니다|확진으로|진단됩니다|진단이 됩니다|틀림없"
    r"|앓고 있|병입니다|질환입니다)")
_RISK_CLAIM = re.compile(
    r"(유병률|위험합니다|위험하다고|위험한 증상|위험성|전조 증상|전조입니다"
    r"|반드시 병원)")
_COMPANY_PRODUCT = re.compile(
    r"(삼성생명|삼성화재|현대해상|DB손해|KB손해|한화생명|교보생명)\s*\S*보험")
# 「추천합니다」단독은 비교 안내용으로 허용. 가입 강요만 차단.
_SOLICIT = re.compile(r"(가입하세요|가입을 권유|가입을 추천)")
# 브리프에 남은 상병코드 괄호·필러 제거
_KCD_PAREN = re.compile(r"\(([A-Z]\d{2}(?:\.\d+)?)\)")
_FILLER_REF = re.compile(
    r"(이 안내는 참고용 안내입니다\.?|이 내용은 참고용 안내입니다\.?"
    r"|참고용 안내입니다\.?)")


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
               catalog_labels: list[str] | None = None,
               keep_phrases: list[str] | None = None) -> tuple[str, list[str]]:
    """(정제된 텍스트, 차단 사유 목록).

    규칙 이탈은 카탈로그 라벨이 이번 요청의 허용 집합 밖에 있을 때만 자른다.
    keep_phrases에 있는 매칭 상품명·보험사는 회사명 패턴에서도 유지한다.
    """
    flags: list[str] = []
    out = text or ""
    keep = [p for p in (keep_phrases or []) if p]
    if _DIAGNOSIS.search(out):
        flags.append("diagnosis_blocked")
        out = _DIAGNOSIS.sub("", out)
    if _RISK_CLAIM.search(out):
        flags.append("risk_claim_blocked")
        out = _RISK_CLAIM.sub("", out)
    if _COMPANY_PRODUCT.search(out):
        blocked = False

        def _company_repl(m: re.Match) -> str:
            nonlocal blocked
            span = m.group(0)
            for phrase in keep:
                if span in phrase or phrase in span:
                    return span
            blocked = True
            return ""

        out = _COMPANY_PRODUCT.sub(_company_repl, out)
        if blocked:
            flags.append("product_name_blocked")
    if _SOLICIT.search(out):
        flags.append("solicit_blocked")
        out = _SOLICIT.sub("", out)
    if _KCD_PAREN.search(out):
        flags.append("kcd_code_stripped")
        out = _KCD_PAREN.sub("", out)
    if _FILLER_REF.search(out):
        flags.append("filler_ref_stripped")
        out = _FILLER_REF.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    # 문장 부호 뒤 공백은 넣되, 소수·코드(R00.2, 0.04)는 깨지 않는다.
    out = re.sub(r"([.，。])(?=[^\d\n]|$)", r"\1 ", out)
    out = re.sub(r" ,", ",", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()

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
