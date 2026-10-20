"""규칙 엔진 — 증상·직업·걱정 포인트로 보장 후보를 결정한다."""
from __future__ import annotations

from dataclasses import dataclass, field

from harag.guidance.lexicon import SymptomHit, load_kb, match_symptoms

OCCUPATIONS = {
    "service": "서비스직",
    "office": "사무직",
    "other": "기타",
}
CONCERNS = {
    "outpatient": "통원비",
    "inpatient": "입원",
    "tests": "검사",
}


@dataclass
class AreaHit:
    label: str
    source: str
    confidence: float


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
    abstained: bool = False
    abstain_reason: str | None = None


def run_rules(masked_text: str, occupation: str | None = None,
              concern: str | None = None,
              symptom_codes: list[str] | None = None,
              kb: dict | None = None) -> RuleResult:
    kb = kb or load_kb()
    occ = occupation if occupation in OCCUPATIONS else None
    con = concern if concern in CONCERNS else None

    if symptom_codes:
        hits = []
        seen: set[str] = set()
        for code in symptom_codes:
            if code in seen:
                continue
            spec = (kb.get("symptoms") or {}).get(code)
            if spec:
                seen.add(code)
                hits.append(SymptomHit(code=code, label=spec["label"], alias=code))
    else:
        hits = match_symptoms(masked_text, kb)

    if not hits:
        return RuleResult(
            symptoms=[], occupation=occ, concern=con, areas=[], coverages=[],
            abstained=True, abstain_reason="unsupported_symptom")

    areas: list[AreaHit] = []
    scores: dict[str, float] = {}
    why: dict[str, list[str]] = {}
    cov_meta = kb.get("coverages") or {}

    def _add(code: str, pts: float, reason: str) -> None:
        if code not in cov_meta:
            return
        scores[code] = scores.get(code, 0.0) + pts
        why.setdefault(code, []).append(reason)

    seen_areas: set[str] = set()
    for hit in hits:
        spec = kb["symptoms"][hit.code]
        for area in spec.get("areas") or []:
            key = area["label"]
            if key in seen_areas:
                continue
            seen_areas.add(key)
            areas.append(AreaHit(
                label=area["label"], source=area.get("source") or "",
                confidence=float(area.get("confidence") or 0),
            ))
        for code in spec.get("coverages") or []:
            _add(code, 1.0, f"증상 '{hit.label}' 기본 매칭")
        if occ:
            for code in (spec.get("occupation_boost") or {}).get(occ) or []:
                _add(code, 0.4, f"직업군 '{OCCUPATIONS[occ]}' 가중")
        if con:
            for code in (spec.get("concern_boost") or {}).get(con) or []:
                _add(code, 0.5, f"관심 '{CONCERNS[con]}' 가중")

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
        symptoms=hits, occupation=occ, concern=con,
        areas=areas, coverages=coverages)
