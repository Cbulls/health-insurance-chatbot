"""상태 안내 서비스 — 마스킹 → 규칙 → (선택) RAG 근거 → 설명 → 가드."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from harag.guidance.engine import (
    CONCERNS, OCCUPATIONS, CoverageHit, RuleResult, run_rules,
)
from harag.guidance.guard import (
    DISCLAIMER, allowed_labels, guard_text, known_coverage_labels,
    split_disclaimer,
)
from harag.security.pii import PiiMasker

logger = logging.getLogger("harag.guidance")


@dataclass
class AssessResult:
    masked_preview: str
    pii_kinds: dict[str, int]
    symptoms: list[dict]
    occupation: str | None
    occupation_label: str | None
    concern: str | None
    concern_label: str | None
    abstained: bool = False
    abstain_reason: str | None = None


@dataclass
class GuidanceResult:
    profile: dict
    areas: list[dict]
    coverages: list[dict]
    explanation: str
    citations: list[str] = field(default_factory=list)
    guard_flags: list[str] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None
    raw_stored: bool = False  # 항상 False — 원문 저장 없음
    disclaimer: str = DISCLAIMER


class GuidanceService:
    def __init__(self, masker: PiiMasker | None = None,
                 pipeline=None, llm=None):
        self._masker = masker or PiiMasker()
        self._pipeline = pipeline
        self._llm = llm

    def assess(self, text: str, occupation: str | None = None,
               concern: str | None = None) -> AssessResult:
        masked, kinds = self._masker.mask(text or "")
        # 원문 `text`는 이 스택 프레임을 벗어나 저장하지 않는다.
        rules = run_rules(masked, occupation=occupation, concern=concern)
        return AssessResult(
            masked_preview=masked,
            pii_kinds=kinds,
            symptoms=[{"code": h.code, "label": h.label} for h in rules.symptoms],
            occupation=rules.occupation,
            occupation_label=OCCUPATIONS.get(rules.occupation or ""),
            concern=rules.concern,
            concern_label=CONCERNS.get(rules.concern or ""),
            abstained=rules.abstained,
            abstain_reason=rules.abstain_reason,
        )

    def guide(self, text: str = "", occupation: str | None = None,
              concern: str | None = None,
              symptom_codes: list[str] | None = None,
              auth=None) -> GuidanceResult:
        # 코드가 있으면 자유문은 버린다. 없으면 마스킹 후 사전 매칭만 한다.
        # LLM·검색·프로필에는 원문을 넣지 않는다.
        if symptom_codes:
            rules = run_rules(
                "", occupation=occupation, concern=concern,
                symptom_codes=symptom_codes)
        else:
            masked, _ = self._masker.mask(text or "")
            rules = run_rules(
                masked, occupation=occupation, concern=concern)
        if rules.abstained:
            return GuidanceResult(
                profile=_profile(rules),
                areas=[], coverages=[], explanation="",
                abstained=True, abstain_reason=rules.abstain_reason,
                raw_stored=False)

        citations: list[str] = []
        if self._pipeline is not None and auth is not None:
            citations = self._lookup_evidence(rules.coverages, auth)

        explanation = self._explain(rules, citations)
        cleaned, flags = guard_text(
            explanation, allowed_labels(rules.coverages), self._masker,
            catalog_labels=known_coverage_labels())
        body, disclaimer = split_disclaimer(cleaned)
        return GuidanceResult(
            profile=_profile(rules),
            areas=[{"label": a.label, "source": a.source,
                    "confidence": a.confidence} for a in rules.areas],
            coverages=[{
                "code": c.code, "label": c.label, "summary": c.summary,
                "score": c.score, "why": c.why,
            } for c in rules.coverages],
            explanation=body,
            citations=citations,
            guard_flags=flags,
            raw_stored=False,
            disclaimer=disclaimer,
        )

    def _explain(self, rules: RuleResult, citations: list[str]) -> str:
        template = _template(rules, citations)
        if self._llm is None or not hasattr(self._llm, "complete"):
            return template
        allowed = ", ".join(c.label for c in rules.coverages)
        prompt_q = (
            "규칙이 고른 보장 유형만 설명하라. 규칙을 벗어난 보장을 추가하지 말고, "
            "진단하지 말며, 사용자 원문이나 식별자를 쓰지 마라. "
            f"다음 보장 유형만 설명하라: {allowed}. "
            "특정 보험사 상품명은 쓰지 마라."
        )
        ctx = [template]
        try:
            answer, _ = self._llm.complete(prompt_q, ctx, ["rule-1"])
            return answer or template
        except Exception as e:  # noqa: BLE001
            logger.warning("guidance llm fallback to template: %s", type(e).__name__)
            return template

    def _lookup_evidence(self, coverages: list[CoverageHit], auth) -> list[str]:
        query = " ".join(c.label for c in coverages[:3]) + " 실손 약관"
        retrieve = getattr(self._pipeline, "_retrieve_sync", None)
        if not callable(retrieve):
            return []
        try:
            chunks = retrieve(query, auth, None)
        except Exception as e:  # noqa: BLE001
            logger.info("guidance evidence skip: %s", type(e).__name__)
            return []
        seen: set[str] = set()
        labels: list[str] = []
        for sc in chunks or []:
            lab = sc.chunk.meta.citation_label
            if lab not in seen:
                seen.add(lab)
                labels.append(lab)
        return labels[:5]


def _profile(rules: RuleResult) -> dict:
    return {
        "masked_preview": "",
        "symptoms": [{"code": h.code, "label": h.label} for h in rules.symptoms],
        "occupation": rules.occupation,
        "occupation_label": OCCUPATIONS.get(rules.occupation or ""),
        "concern": rules.concern,
        "concern_label": CONCERNS.get(rules.concern or ""),
    }


def _template(rules: RuleResult, citations: list[str]) -> str:
    lines = ["증상: " + ", ".join(h.label for h in rules.symptoms)]
    if rules.occupation:
        lines.append("직업군: " + OCCUPATIONS[rules.occupation])
    if rules.concern:
        lines.append("관심 보장: " + CONCERNS[rules.concern])
    lines.append("")
    lines.append("가능한 관련 영역(확진 아님):")
    for a in rules.areas:
        conf = f"{a.confidence:.2f}"
        lines.append(f"- {a.label} (신뢰 {conf}; 출처: {a.source})")
    lines.append("")
    lines.append("살펴볼 보장 유형:")
    for c in rules.coverages:
        lines.append(f"- {c.label}: {c.summary}")
        lines.append("  이유: " + "; ".join(c.why))
    if citations:
        lines.append("")
        lines.append("업로드한 약관에서 찾은 근거: " + "; ".join(citations))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
