"""규칙 엔진 — 증상·직업·걱정 포인트로 보장 후보를 결정한다."""
from __future__ import annotations

from dataclasses import dataclass, field

from harag.guidance.lexicon import SymptomHit, load_kb, match_symptoms

OCCUPATIONS = {
    "office": "사무·관리직",
    "service": "고객응대·판매·요식",
    "field": "현장·운전·배달",
    "agriculture": "농업 종사자",
    "fishery": "어업 종사자",
    "care_health": "의료·돌봄",
    "education": "교육·연구",
    "homemaker": "가사·육아(무급)",
    "retired": "은퇴·무직",
    "student": "학생",
    "other": "기타",
}
CONCERNS = {
    "outpatient": "병원비 부담",
    "inpatient": "입원·수술 비용",
    "tests": "장기 치료",
}


@dataclass
class AreaHit:
    label: str
    source: str
    confidence: float
    matched: int = 0
    selected: int = 0


@dataclass
class CoverageHit:
    code: str
    label: str
    summary: str
    score: float
    why: list[str] = field(default_factory=list)


@dataclass
class RuleResult:
    symptoms: list[SymptomHit]
    occupation: str | None
    concern: str | None
    areas: list[AreaHit]
    coverages: list[CoverageHit]
    concerns: list[str] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None


def _normalize_concerns(concern: str | None,
                         concerns: list[str] | None) -> list[str]:
    raw = list(concerns or [])
    if concern:
        raw.append(concern)
    seen: set[str] = set()
    out: list[str] = []
    for code in raw:
        if code == "none":
            return []
        if code in CONCERNS and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def run_rules(masked_text: str, occupation: str | None = None,
              concern: str | None = None,
              concerns: list[str] | None = None,
              symptom_codes: list[str] | None = None,
              kb: dict | None = None) -> RuleResult:
    kb = kb or load_kb()
    occ = occupation if occupation in OCCUPATIONS else None
    cons = _normalize_concerns(concern, concerns)
    primary = cons[0] if cons else None

    if symptom_codes:
        hits = []
        seen: set[str] = set()
        for code in symptom_codes:
            if code in seen:
                continue
            spec = (kb.get("symptoms") or {}).get(code)
            if spec:
                seen.add(code)
                hits.append(SymptomHit(
                    code=code, label=spec["label"], alias=code,
                    type=str(spec.get("type") or "usual")))
    else:
        hits = match_symptoms(masked_text, kb)

    if not hits:
        return RuleResult(
            symptoms=[], occupation=occ, concern=primary, concerns=cons,
            areas=[], coverages=[],
            abstained=True, abstain_reason="unsupported_symptom")

    scores: dict[str, float] = {}
    why: dict[str, list[str]] = {}
    cov_meta = kb.get("coverages") or {}
    area_codes: dict[str, list[str]] = {}
    area_meta: dict[str, tuple[str, float]] = {}

    def _add(code: str, pts: float, reason: str) -> None:
        if code not in cov_meta:
            return
        scores[code] = scores.get(code, 0.0) + pts
        why.setdefault(code, []).append(reason)

    for hit in hits:
        spec = kb["symptoms"][hit.code]
        for area in spec.get("areas") or []:
            key = area["label"]
            area_codes.setdefault(key, [])
            if hit.code not in area_codes[key]:
                area_codes[key].append(hit.code)
            if key not in area_meta:
                area_meta[key] = (
                    area.get("source") or "",
                    float(area.get("confidence") or 0),
                )
        for code in spec.get("coverages") or []:
            _add(code, 1.0, f"증상 '{hit.label}' 기본 매칭")
        if occ:
            for code in (spec.get("occupation_boost") or {}).get(occ) or []:
                _add(code, 0.4, f"직업군 '{OCCUPATIONS[occ]}' 가중")
        for con in cons:
            for code in (spec.get("concern_boost") or {}).get(con) or []:
                _add(code, 0.5, f"관심 '{CONCERNS[con]}' 가중")

    selected_n = len(hits)
    areas = [
        AreaHit(
            label=label,
            source=area_meta[label][0],
            confidence=area_meta[label][1],
            matched=len(codes),
            selected=selected_n,
        )
        for label, codes in area_codes.items()
    ]
    areas.sort(key=lambda a: (-a.matched, a.label))

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    coverages = [
        CoverageHit(
            code=code,
            label=cov_meta[code]["label"],
            summary=cov_meta[code]["summary"],
            score=round(pts, 3),
            why=why[code],
        )
        for code, pts in ranked
    ]
    return RuleResult(
        symptoms=hits, occupation=occ, concern=primary, concerns=cons,
        areas=areas, coverages=coverages)
