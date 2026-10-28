"""상태 안내 서비스 — 마스킹 → 규칙 → (선택) RAG 근거 → 설명 → 가드."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from harag.guidance.engine import (
    CONCERNS, OCCUPATIONS, CoverageHit, RuleResult, run_rules,
)
from harag.guidance.lexicon import load_kb, symptom_payload
from harag.guidance.guard import (
    DISCLAIMER, allowed_labels, guard_text, known_coverage_labels,
    split_disclaimer,
)
from harag.security.pii import PiiMasker

logger = logging.getLogger("harag.guidance")

AGE_QUERY_LABELS = {
    "0-9": "9세 이하",
    "10-19": "10대",
    "20-29": "20대",
    "30-39": "30대",
    "40-49": "40대",
    "50-59": "50대",
    "60-69": "60대",
    "70+": "70대 이상",
}
SEX_QUERY_LABELS = {
    "male": "남성",
    "female": "여성",
}

# 상품 설명서에 '실손 통원'이 없어도 카드에 규칙 라벨을 걸기 위한 동의어.
# 사용자가 쓴 문장이 아니라 문서에 이미 있는 담보명만.
COVERAGE_ALIASES = {
    "실손 통원": ["통원의료비", "상해통원", "질병통원", "외래"],
    "실손 입원": ["입원의료비", "상해입원", "질병입원", "입원일당"],
    "실손 검사": ["골절진단비", "화상진단비", "진단비", "검사비"],
    "상해 관련 실손": ["일반상해", "상해의료비", "교통상해", "상해후유장해"],
}


@dataclass
class AssessResult:
    masked_preview: str
    pii_kinds: dict[str, int]
    symptoms: list[dict]
    occupation: str | None
    occupation_label: str | None
    concern: str | None
    concern_label: str | None
    concerns: list[str] = field(default_factory=list)
    concern_labels: list[str] = field(default_factory=list)
    age_band: str | None = None
    sex: str | None = None
    abstained: bool = False
    abstain_reason: str | None = None


@dataclass
class GuidanceResult:
    profile: dict
    areas: list[dict]
    coverages: list[dict]
    explanation: str
    explanation_why: str = ""
    explanation_caution: str = ""
    citations: list[str] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    ref_stats: list[dict] = field(default_factory=list)
    guard_flags: list[str] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None
    raw_stored: bool = False  # 항상 False — 원문 저장 없음
    disclaimer: str = DISCLAIMER


class GuidanceService:
    def __init__(self, masker: PiiMasker | None = None,
                 pipeline=None, llm=None, stats=None):
        self._masker = masker or PiiMasker()
        self._pipeline = pipeline
        self._llm = llm
        self._stats = stats

    def assess(self, text: str, occupation: str | None = None,
               concern: str | None = None,
               concerns: list[str] | None = None,
               age_band: str | None = None,
               sex: str | None = None) -> AssessResult:
        masked, kinds = self._masker.mask(text or "")
        # 원문 `text`는 이 스택 프레임을 벗어나 저장하지 않는다.
        rules = run_rules(
            masked, occupation=occupation, concern=concern, concerns=concerns)
        return AssessResult(
            masked_preview=masked,
            pii_kinds=kinds,
            symptoms=_symptom_items(rules),
            occupation=rules.occupation,
            occupation_label=OCCUPATIONS.get(rules.occupation or ""),
            concern=rules.concern,
            concern_label=CONCERNS.get(rules.concern or ""),
            concerns=rules.concerns,
            concern_labels=[CONCERNS[c] for c in rules.concerns],
            age_band=age_band,
            sex=sex if sex in SEX_QUERY_LABELS or sex == "unspecified" else None,
            abstained=rules.abstained,
            abstain_reason=rules.abstain_reason,
        )

    def guide(self, text: str = "", occupation: str | None = None,
              concern: str | None = None,
              concerns: list[str] | None = None,
              symptom_codes: list[str] | None = None,
              age_band: str | None = None,
              sex: str | None = None,
              auth=None) -> GuidanceResult:
        # 코드가 있으면 자유문은 버린다. 없으면 마스킹 후 사전 매칭만 한다.
        # LLM·프로필에는 원문을 넣지 않는다.
        # age_band·sex는 닫힌 검색 질의에만 쓰고 설명 문장에는 쓰지 않는다.
        if symptom_codes:
            rules = run_rules(
                "", occupation=occupation, concern=concern,
                concerns=concerns, symptom_codes=symptom_codes)
        else:
            masked, _ = self._masker.mask(text or "")
            rules = run_rules(
                masked, occupation=occupation, concern=concern,
                concerns=concerns)
        if rules.abstained:
            return GuidanceResult(
                profile=_profile(rules, age_band=age_band, sex=sex),
                areas=[], coverages=[], explanation="",
                abstained=True, abstain_reason=rules.abstain_reason,
                raw_stored=False)

        citations: list[str] = []
        products: list[dict] = []
        if self._pipeline is not None and auth is not None:
            citations, products = self._lookup_evidence(
                rules.coverages, auth,
                occupation=occupation, age_band=age_band, sex=sex)

        why_raw, _caution_unused = self._explain(
            rules, citations, products=products)
        allowed = allowed_labels(rules.coverages)
        catalog = known_coverage_labels()
        keep = _product_keep_phrases(products)
        why, why_flags = guard_text(
            why_raw, allowed, self._masker, catalog_labels=catalog,
            keep_phrases=keep)
        why_body, disclaimer = split_disclaimer(why)
        return GuidanceResult(
            profile=_profile(rules, age_band=age_band, sex=sex),
            areas=[{"label": a.label, "source": a.source,
                    "confidence": a.confidence,
                    "matched": a.matched, "selected": a.selected}
                   for a in rules.areas],
            coverages=[{
                "code": c.code, "label": c.label, "summary": c.summary,
                "score": c.score, "why": c.why,
            } for c in rules.coverages],
            explanation=why_body,
            explanation_why=why_body,
            explanation_caution="",
            citations=citations,
            products=products,
            ref_stats=[],
            guard_flags=why_flags,
            raw_stored=False,
            disclaimer=disclaimer,
        )

    def _lookup_stats(self, rules: RuleResult, age_band: str | None,
                      sex: str | None) -> list[dict]:
        if self._stats is None:
            return []
        lookup = getattr(self._stats, "lookup", None)
        if not callable(lookup):
            return []
        try:
            return list(lookup(rules, age_band=age_band, sex=sex) or [])
        except Exception as e:  # noqa: BLE001
            logger.info("guidance stats skip: %s", type(e).__name__)
            return []

    def _explain(self, rules: RuleResult, citations: list[str],
                 products: list[dict] | None = None) -> tuple[str, str]:
        why_fb = _why_fallback(rules, products)
        facts = _facts(rules, citations, products)
        if self._llm is None or not hasattr(self._llm, "complete"):
            return why_fb, ""
        allowed = ", ".join(c.label for c in rules.coverages)
        product_names = [
            (p.get("name") or p.get("title") or "").strip()
            for p in (products or [])
            if (p.get("name") or p.get("title") or "").strip()
        ]
        product_clause = (
            "사실에 적힌 매칭 상품만 상품명으로 언급하고, "
            "증상·보장 유형과 그 상품의 matched 담보가 왜 연결되는지 "
            "「관련이 있습니다」체로 적어라. "
            if product_names else
            "상품명은 쓰지 마라. 선택한 불편·관심과 보장 유형이 왜 연결되는지 "
            "「관련이 있습니다」체로 적어라. "
        )
        prompt_q = (
            "아래 사실만 사용해 ## 맞는 이유 블록만 출력하라. "
            "모든 문장은 존댓말(합니다/하세요/있습니다)로 쓰고, "
            "명사형 종결(~임, ~함, ~이다, ~맞음)과 [문서 N] 마커는 쓰지 마라. "
            "첫 줄에는 자주 느끼는 증상과 놓치기 쉬운 불편을 함께 반영했는지 "
            "짧게 밝히고, 이어서 "
            + product_clause +
            "진단하지 마라. 좋다·추천·가입 같은 권유를 쓰지 마라. "
            f"다음 보장 유형만 언급하라: {allowed}. "
            "사용자 원문이나 식별자를 쓰지 마라. "
            "사실에 연령이 없으면 연령 문장을 쓰지 마라."
        )
        try:
            answer, _ = self._llm.complete(prompt_q, [facts], ["rule-1"])
            why, _ = _parse_why_caution(answer, why_fb, "")
            return _strip_doc_markers(why), ""
        except Exception as e:  # noqa: BLE001
            logger.warning("guidance llm fallback to template: %s", type(e).__name__)
            return why_fb, ""

    def _lookup_evidence(
            self, coverages: list[CoverageHit], auth,
            occupation: str | None = None,
            age_band: str | None = None,
            sex: str | None = None,
    ) -> tuple[list[str], list[dict]]:
        query = closed_product_query(coverages, occupation, age_band, sex)
        chunks = []
        retrieve = getattr(self._pipeline, "_retrieve_sync", None)
        if callable(retrieve):
            try:
                raw = retrieve(query, auth, None)
                # QueryPipeline._retrieve_sync는 (chunks, trace) 튜플을 준다.
                chunks = raw[0] if isinstance(raw, tuple) else raw
            except Exception as e:  # noqa: BLE001
                logger.info("guidance evidence skip: %s", type(e).__name__)
        citations = _citations_from_chunks(chunks)
        products = _products_from_chunks(chunks, coverages)
        products = _fill_library_products(self._pipeline, products, coverages)
        products = _attach_brochure_profiles(self._pipeline, products)
        for p in products:
            p["snippet"] = ""
        return citations, products


def closed_product_query(
        coverages: list[CoverageHit],
        occupation: str | None,
        age_band: str | None,
        sex: str | None) -> str:
    """보장 라벨·직업·연령대·성별만. 사용자 자유문·식별자 없음."""
    parts = [c.label for c in coverages]
    for c in coverages:
        parts.extend(COVERAGE_ALIASES.get(c.label, [])[:3])
    occ = OCCUPATIONS.get(occupation or "")
    if occ:
        parts.append(occ)
    age = AGE_QUERY_LABELS.get(age_band or "")
    if age:
        parts.append(age)
    sx = SEX_QUERY_LABELS.get(sex or "")
    if sx:
        parts.append(sx)
    parts.append("보장")
    return " ".join(parts)


def _citations_from_chunks(chunks) -> list[str]:
    seen: set[str] = set()
    labels: list[str] = []
    for sc in chunks or []:
        chunk = getattr(sc, "chunk", None)
        meta = getattr(chunk, "meta", None) if chunk is not None else None
        lab = getattr(meta, "citation_label", None)
        if lab and lab not in seen:
            seen.add(lab)
            labels.append(lab)
    return labels[:5]


def _snippet(text: str, limit: int = 220) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _matched_coverages(text: str, labels: list[str]) -> list[str]:
    blob = text or ""
    out: list[str] = []
    for lab in labels:
        if not lab:
            continue
        if lab in blob or any(a in blob for a in COVERAGE_ALIASES.get(lab, [])):
            out.append(lab)
    return out


def _chunk_scope(meta) -> str:
    tags = list(getattr(meta, "acl_tags", None) or [])
    if "scope:library" in tags or "lib:knowledge" in tags:
        return "library"
    if "scope:shared" in tags:
        return "shared"
    return "personal"


def _rank_retrieved_products(items: list[dict]) -> list[dict]:
    ranked = sorted(
        items,
        key=lambda p: (len(p.get("matched_coverages") or []), p.get("score") or 0),
        reverse=True,
    )
    out = []
    for p in ranked[:3]:
        out.append({k: v for k, v in p.items() if k != "score"})
    return out


def _products_from_chunks(chunks, coverages: list[CoverageHit]) -> list[dict]:
    labels = [c.label for c in coverages]
    best: dict[str, dict] = {}
    for sc in chunks or []:
        chunk = getattr(sc, "chunk", None)
        meta = getattr(chunk, "meta", None) if chunk is not None else None
        if meta is None:
            continue
        doc_id = getattr(meta, "document_id", None) or ""
        if not doc_id:
            continue
        score = float(getattr(sc, "score", 0) or 0)
        prev = best.get(doc_id)
        if prev is not None and score <= prev["score"]:
            continue
        text = getattr(chunk, "text", "") or ""
        title = getattr(meta, "source_document", None) or doc_id
        citation = getattr(meta, "citation_label", None) or ""
        matched = _matched_coverages(text, labels)
        best[doc_id] = {
            "document_id": doc_id,
            "title": title,
            "citation_label": citation,
            "snippet": _snippet(text),
            "matched_coverages": matched,
            "scope": _chunk_scope(meta),
            "score": score,
        }
    return _rank_retrieved_products(list(best.values()))


def _coverage_fallback_snippet(coverages: list[CoverageHit]) -> str:
    parts = [f"{c.label}: {c.summary}" for c in coverages if c.summary]
    return _snippet(" ".join(parts))


def _fill_library_products(
        pipeline, products: list[dict],
        coverages: list[CoverageHit]) -> list[dict]:
    """검색에 없던 공용 ready 행을 남은 칸에 채운다. 검색 히트가 우선."""
    if len(products) >= 3:
        return products
    meta_store = getattr(pipeline, "_metadata", None)
    list_fn = getattr(meta_store, "list_library_documents", None)
    if not callable(list_fn):
        return products
    try:
        rows = list_fn()
    except Exception:  # noqa: BLE001
        logger.info("library product list skip")
        return products
    seen = {p.get("document_id") for p in products}
    snippet = _coverage_fallback_snippet(coverages)
    labels = [c.label for c in coverages]
    filled = list(products)
    for rec in rows or []:
        if len(filled) >= 3:
            break
        if getattr(rec, "status", "") != "ready":
            continue
        doc_id = getattr(rec, "document_id", "") or ""
        if not doc_id or doc_id in seen:
            continue
        title = getattr(rec, "filename", None) or doc_id
        filled.append({
            "document_id": doc_id,
            "title": title,
            "citation_label": title,
            "snippet": snippet,
            "matched_coverages": list(labels),
            "scope": "library",
        })
        seen.add(doc_id)
    return filled


def _attach_brochure_profiles(pipeline, products: list[dict]) -> list[dict]:
    from harag.guidance.brochure import labels_for
    meta_store = getattr(pipeline, "_metadata", None)
    getter = getattr(meta_store, "get_brochure_profile", None)
    if not callable(getter):
        return products
    out = []
    for p in products:
        rec = None
        try:
            rec = getter(p.get("document_id") or "")
        except Exception:  # noqa: BLE001
            rec = None
        if rec is None:
            out.append(p)
            continue
        labs = labels_for({
            "display_name": rec.display_name,
            "product_line": rec.product_line,
            "doc_kind": rec.doc_kind,
            "cost_nature": rec.cost_nature,
            "summary": rec.summary,
            "insurer": rec.insurer,
            "source_url": rec.source_url,
        })
        merged = dict(p)
        merged["name"] = labs["display_name"] or p.get("title") or ""
        merged["product_line"] = labs["product_line"]
        merged["product_line_label"] = labs["product_line_label"]
        merged["doc_kind_label"] = labs["doc_kind_label"]
        merged["cost_nature_label"] = labs["cost_nature_label"]
        merged["insurer"] = labs["insurer"]
        merged["source_url"] = labs["source_url"]
        merged["summary"] = labs["summary"]
        merged["snippet"] = ""
        out.append(merged)
    return out


def _profile(rules: RuleResult, age_band: str | None = None,
             sex: str | None = None) -> dict:
    sex_n = sex if sex in SEX_QUERY_LABELS else None
    return {
        "masked_preview": "",
        "symptoms": _symptom_items(rules),
        "occupation": rules.occupation,
        "occupation_label": OCCUPATIONS.get(rules.occupation or ""),
        "concern": rules.concern,
        "concern_label": CONCERNS.get(rules.concern or ""),
        "concerns": rules.concerns,
        "concern_labels": [CONCERNS[c] for c in rules.concerns],
        "age_band": age_band if age_band in AGE_QUERY_LABELS else None,
        "age_label": AGE_QUERY_LABELS.get(age_band or ""),
        "sex": sex_n,
        "sex_label": SEX_QUERY_LABELS.get(sex_n or ""),
    }


def _symptom_items(rules: RuleResult) -> list[dict]:
    kb = load_kb()
    specs = kb.get("symptoms") or {}
    return [
        symptom_payload(h.code, h.label, h.type, specs.get(h.code))
        for h in rules.symptoms
    ]


def _symptom_type_labels(rules: RuleResult) -> tuple[str, str]:
    usual = [h.label for h in rules.symptoms if (h.type or "usual") != "hidden"]
    hidden = [h.label for h in rules.symptoms if h.type == "hidden"]
    usual_s = ", ".join(usual) if usual else "해당 없음"
    hidden_s = ", ".join(hidden) if hidden else "해당 없음"
    return usual_s, hidden_s


def _product_keep_phrases(products: list[dict] | None) -> list[str]:
    out: list[str] = []
    for p in products or []:
        for key in ("name", "title", "insurer", "display_name"):
            val = str(p.get(key) or "").strip()
            if val and val not in out:
                out.append(val)
    return out


def _why_fallback(rules: RuleResult,
                  products: list[dict] | None = None) -> str:
    lines: list[str] = []
    usual_s, hidden_s = _symptom_type_labels(rules)
    if rules.symptoms:
        lines.append(
            "자주 느끼는 증상과 놓치기 쉬운 불편을 함께 반영해 "
            "보장 종류를 골랐습니다."
        )
        lines.append(f"자주 느끼는 증상: {usual_s}.")
        lines.append(f"놓치기 쉬운 불편: {hidden_s}.")
    for c in rules.coverages:
        reason = "; ".join(c.why)
        if reason:
            lines.append(
                f"「{c.label}」은 {reason} 때문에 관련이 있습니다.")
        else:
            lines.append(f"「{c.label}」을 살펴보시면 됩니다.")
    for p in (products or [])[:3]:
        name = (p.get("name") or p.get("title") or "").strip()
        if not name:
            continue
        kinds = p.get("matched_coverages") or []
        if kinds:
            joined = ", ".join(str(k) for k in kinds)
            lines.append(
                f"「{name}」은 설명서에 {joined} 관련 담보가 있어 "
                "선택한 증상·보장과 관련이 있습니다.")
        else:
            lines.append(
                f"「{name}」은 선택한 보장 종류와 관련 담보를 "
                "안내하는 상품요약서입니다.")
    return "\n".join(lines)


_DOC_MARKER_RE = re.compile(r"\[\s*문서\s*\d+(?:\s*,\s*\d+)*\s*\]")


def _strip_doc_markers(text: str) -> str:
    out = _DOC_MARKER_RE.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _parse_why_caution(text: str, why_fb: str, caution_fb: str) -> tuple[str, str]:
    raw = (text or "").replace(DISCLAIMER, "").strip()
    if not raw:
        return why_fb, caution_fb
    why_m = re.search(r"##\s*맞는 이유\s*(.*?)(?=##\s*알아둘 점|$)", raw, re.S)
    caution_m = re.search(r"##\s*알아둘 점\s*(.*)$", raw, re.S)
    why = (why_m.group(1).strip() if why_m else "")
    caution = (caution_m.group(1).strip() if caution_m else "")
    if not why and not caution:
        return _strip_doc_markers(raw), caution_fb
    return _strip_doc_markers(why or why_fb), caution or caution_fb


def _facts(rules: RuleResult, citations: list[str],
           products: list[dict] | None = None) -> str:
    return _template(rules, citations, products)


def _template(rules: RuleResult, citations: list[str],
              products: list[dict] | None = None) -> str:
    usual_s, hidden_s = _symptom_type_labels(rules)
    lines = [
        "자주 느끼는 증상: " + usual_s,
        "놓치기 쉬운 불편: " + hidden_s,
    ]
    if rules.occupation:
        lines.append("직업군: " + OCCUPATIONS[rules.occupation])
    if rules.concerns:
        lines.append("관심 보장: " + ", ".join(CONCERNS[c] for c in rules.concerns))
    lines.append("")
    lines.append("살펴볼 보장 유형:")
    for c in rules.coverages:
        lines.append(f"- {c.label}: {c.summary}")
        lines.append("  이유: " + "; ".join(c.why))
    if products:
        lines.append("")
        lines.append("매칭 상품(가입 권유 아님, 설명서 비교용):")
        for p in products[:3]:
            name = (p.get("name") or p.get("title") or "").strip() or "상품"
            insurer = (p.get("insurer") or "").strip()
            kinds = p.get("matched_coverages") or []
            summary = (p.get("summary") or "").strip()[:160]
            head = f"- {name}"
            if insurer:
                head += f" ({insurer})"
            lines.append(head)
            if kinds:
                lines.append("  matched: " + ", ".join(str(k) for k in kinds))
            if summary:
                lines.append("  summary: " + summary)
    if citations:
        lines.append("")
        lines.append("업로드한 약관에서 찾은 근거: " + "; ".join(citations))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
