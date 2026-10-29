"""상태 안내 서비스 — 마스킹 → 규칙 → (선택) RAG 근거 → 설명 → 가드."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from harag.guidance.engine import (
    CONCERNS, OCCUPATIONS, CoverageHit, RuleResult, run_rules,
)
from harag.guidance.lexicon import (
    load_kb, portal_overview_of, ref_conditions_of, symptom_payload,
)
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

# RESULT에 보여 줄 비교 상품 수. 후보는 더 모아 점수·다양성으로 고른다.
PRODUCT_COMPARE_CAP = 2
PRODUCT_CANDIDATE_CAP = 8


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
    symptom_clusters: list[dict] = field(default_factory=list)
    cluster_stats: list[dict] = field(default_factory=list)
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
                rules, auth,
                occupation=occupation, age_band=age_band, sex=sex)

        ref_stats = self._lookup_stats(rules, age_band=age_band, sex=sex)
        codes = [h.code for h in rules.symptoms]
        from harag.guidance.clusters import (
            cluster_example_kcds, match_symptom_clusters,
        )
        symptom_clusters = match_symptom_clusters(codes)
        cluster_stats = self._lookup_cluster_stats(
            symptom_clusters, rules, age_band=age_band, sex=sex)
        why_raw, _caution_unused = self._explain(
            rules, citations, products=products, ref_stats=ref_stats,
            age_band=age_band, sex=sex,
            symptom_clusters=symptom_clusters,
            cluster_stats=cluster_stats)
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
            ref_stats=ref_stats,
            symptom_clusters=symptom_clusters,
            cluster_stats=cluster_stats,
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

    def _lookup_cluster_stats(
            self, clusters: list[dict], rules: RuleResult,
            age_band: str | None, sex: str | None) -> list[dict]:
        if self._stats is None or not clusters:
            return []
        from harag.guidance.clusters import cluster_example_kcds
        kcds = cluster_example_kcds(clusters)
        if not kcds:
            return []
        lookup = getattr(self._stats, "lookup_kcds", None)
        if not callable(lookup):
            return []
        labels = [c.label for c in rules.coverages]
        try:
            return list(lookup(
                kcds, age_band=age_band, sex=sex, labels=labels) or [])
        except Exception as e:  # noqa: BLE001
            logger.info("guidance cluster stats skip: %s", type(e).__name__)
            return []

    def _explain(self, rules: RuleResult, citations: list[str],
                 products: list[dict] | None = None,
                 ref_stats: list[dict] | None = None,
                 age_band: str | None = None,
                 sex: str | None = None,
                 symptom_clusters: list[dict] | None = None,
                 cluster_stats: list[dict] | None = None,
                 ) -> tuple[str, str]:
        why_fb = _why_fallback(
            rules, products, ref_stats=ref_stats,
            age_band=age_band, sex=sex,
            symptom_clusters=symptom_clusters,
            cluster_stats=cluster_stats)
        facts = _facts(
            rules, citations, products, ref_stats=ref_stats,
            age_band=age_band, sex=sex,
            symptom_clusters=symptom_clusters,
            cluster_stats=cluster_stats)
        if self._llm is None or not hasattr(self._llm, "complete"):
            return why_fb, ""
        allowed = ", ".join(c.label for c in rules.coverages)
        product_names = [
            (p.get("name") or p.get("title") or "").strip()
            for p in (products or [])
            if (p.get("name") or p.get("title") or "").strip()
        ]
        product_clause = (
            "## 비교할 상품 2개 섹션에서는 사실에 적힌 상품만 이름으로 언급하고, "
            "각 상품이 증상·보장과 왜 맞는지 1~2문장으로 적어라. "
            "이 섹션 밖에는 상품명을 쓰지 마라. "
            if product_names else
            "## 비교할 상품 2개 섹션에는 상품명이 없음을 짧게 알리고 "
            "보장 유형만 정리하라. "
        )
        prompt_q = (
            "너는 공공 참고 자료와 보험 보장을 연결하는 보험 안내 전문가다. "
            "확진·진단 단정·모집·가입 강요는 하지 않는다. "
            "아래 사실만 보고 세 헤딩을 이 순서로 모두 출력하라: "
            "## 공공데이터 해석 / ## 주의·참고 / ## 비교할 상품 2개. "
            "모든 문장은 존댓말(합니다/습니다)로 쓰고, "
            "명사형 종결(~임, ~함, ~이다)과 [문서 N] 마커는 쓰지 마라. "
            "유병률·위험하다·위험성·전조·확진됩니다·진단됩니다·confidence 숫자는 쓰지 마라. "
            "「참고용 안내입니다」「이 안내는 참고용」 같은 필러 문장은 쓰지 마라. "
            "KCD·상병코드·(R53) 같은 괄호 코드는 브리프에 쓰지 마라. 상병명은 한글만. "
            "## 공공데이터 해석은 딱 세 짧은 문단만 써라. "
            "1) 반드시 「질병관리청 국가건강정보포털」을 명시하고, "
            "자주 느끼는 증상과 놓치기 쉬운 불편을 한글 상병명·가능 영역"
            "(있으면 증상 묶음 관련 가능 영역)으로만 논리 연결하라. 확진·전조 금지. "
            "2) 반드시 「건강보험심사평가원 질병정보서비스」를 명시하고, "
            "사실의 저장본/연령·성별 코호트가 있으면 그에 맞춰 "
            "외래·입원 청구 패턴만 요약하라. 원시 건수·개요 전문 반복 금지. "
            "자세한 숫자는 아래 공공 카드에 있다고 한 줄로만 안내하라. "
            "3) 직업·걱정·연령·성별(사실에 있을 때만)을 "
            "통원·검사·입원 등 보장 축과 보험 실무 논리로 연결하라. "
            "직업별 질환 유병 통계가 사실에 없으면 만들지 마라. "
            "portal_overview 전문을 다시 쓰지 마라. "
            "## 주의·참고는 확진이 아님·가입 권유가 아님만 두 문장 이내로. "
            "면책 문장을 길게 재서술하지 마라. "
            + product_clause +
            "「가입하세요」「가입을 추천」「가입을 권유」는 쓰지 마라. "
            f"보장 유형은 다음만 언급하라: {allowed}. "
            "사용자 원문이나 식별자를 쓰지 마라."
        )
        try:
            answer, _ = self._llm.complete(prompt_q, [facts], ["rule-1"])
            why = _parse_brief(answer, why_fb)
            return _strip_doc_markers(why), ""
        except Exception as e:  # noqa: BLE001
            logger.warning("guidance llm fallback to template: %s", type(e).__name__)
            return why_fb, ""

    def _lookup_evidence(
            self, rules: RuleResult, auth,
            occupation: str | None = None,
            age_band: str | None = None,
            sex: str | None = None,
    ) -> tuple[list[str], list[dict]]:
        coverages = rules.coverages
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
        products = _enrich_matched_from_codes(products, coverages)
        products = _select_compare_products(
            products, coverages,
            occupation=rules.occupation, concerns=rules.concerns)
        for p in products:
            p["snippet"] = ""
            p.pop("score", None)
            p.pop("_fit", None)
            p.pop("_library_fill", None)
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
    return ranked[:PRODUCT_CANDIDATE_CAP]


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
    """검색에 없던 공용 ready 행을 후보로 채운다. 검색 히트가 우선."""
    if len(products) >= PRODUCT_CANDIDATE_CAP:
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
    filled = list(products)
    for rec in rows or []:
        if len(filled) >= PRODUCT_CANDIDATE_CAP:
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
            # 담보는 프로필 coverage_codes로 나중에 채운다(전체 라벨 부여 금지).
            "matched_coverages": [],
            "scope": "library",
            "score": 0.0,
            "_library_fill": True,
        })
        seen.add(doc_id)
    return filled


def _product_blob(p: dict) -> str:
    parts = [
        str(p.get("name") or ""),
        str(p.get("title") or ""),
        str(p.get("summary") or ""),
        str(p.get("insurer") or ""),
        str(p.get("cost_nature") or ""),
        str(p.get("cost_nature_label") or ""),
        str(p.get("product_line") or ""),
        " ".join(str(x) for x in (p.get("matched_coverages") or [])),
        " ".join(str(x) for x in (p.get("coverage_codes") or [])),
    ]
    return " ".join(parts)


def _enrich_matched_from_codes(
        products: list[dict], coverages: list[CoverageHit]) -> list[dict]:
    code_to_label = {c.code: c.label for c in coverages}
    out = []
    for p in products:
        row = dict(p)
        labels = list(row.get("matched_coverages") or [])
        for code in row.get("coverage_codes") or []:
            lab = code_to_label.get(code)
            if lab and lab not in labels:
                labels.append(lab)
        # 라이브러리 폴백만 있고 코드도 없으면 규칙 라벨을 약하게 힌트로만 둔다.
        if not labels and row.get("_library_fill"):
            labels = [c.label for c in coverages[:2]]
        row["matched_coverages"] = labels
        out.append(row)
    return out


def _product_fit_score(
        p: dict, coverages: list[CoverageHit],
        occupation: str | None, concerns: list[str] | None) -> float:
    labels = [c.label for c in coverages]
    codes = {c.code for c in coverages}
    matched = p.get("matched_coverages") or []
    cov_codes = set(p.get("coverage_codes") or [])
    score = float(p.get("score") or 0) * 5.0
    if not p.get("_library_fill"):
        score += 10.0  # 검색 히트가 공용 폴백보다 우선
    score += 2.0 * sum(1 for m in matched if m in labels)
    score += 1.5 * len(codes & cov_codes)
    blob = _product_blob(p)
    concern_set = set(concerns or [])
    if "inpatient" in concern_set:
        if any(k in blob for k in ("입원", "수술", "실손 입원", "inpatient")):
            score += 3.0
        if (p.get("cost_nature") or "") in ("mixed", "indemnity"):
            score += 2.0
    if "tests" in concern_set:
        if any(k in blob for k in ("장기", "진단", "검사", "암", "tests")):
            score += 2.5
    if "outpatient" in concern_set:
        if any(k in blob for k in ("통원", "외래", "실손 통원", "outpatient")):
            score += 2.0
    if occupation == "field":
        if any(k in blob for k in ("상해", "injury", "재해", "운전")):
            score += 2.5
        if "injury" in cov_codes or "상해 관련 실손" in matched:
            score += 1.0
    if (p.get("cost_nature") or "") == "mixed":
        score += 1.0
    return score


def _nature_bucket(p: dict) -> str:
    cost = (p.get("cost_nature") or "").strip()
    if cost in ("mixed", "indemnity"):
        return "indemnity_side"
    if cost == "lump_sum":
        return "lump_side"
    line = (p.get("product_line") or "").strip()
    if line == "indemnity":
        return "indemnity_side"
    return "other"


def _select_compare_products(
        products: list[dict], coverages: list[CoverageHit],
        occupation: str | None = None,
        concerns: list[str] | None = None) -> list[dict]:
    """점수 + 비용성격 다양성으로 비교 상품 2개를 고른다."""
    if not products:
        return []
    scored: list[dict] = []
    for p in products:
        row = dict(p)
        row["_fit"] = _product_fit_score(
            row, coverages, occupation, concerns)
        scored.append(row)
    scored.sort(
        key=lambda p: (p.get("_fit") or 0, p.get("score") or 0),
        reverse=True)
    picked: list[dict] = []
    used_buckets: set[str] = set()
    for p in scored:
        if len(picked) >= PRODUCT_COMPARE_CAP:
            break
        bucket = _nature_bucket(p)
        if picked and bucket in used_buckets and bucket != "other":
            # 같은 성격만 쌓이면 다음 후보를 본다. 후보가 부족하면 아래에서 채움.
            continue
        picked.append(p)
        used_buckets.add(bucket)
    if len(picked) < PRODUCT_COMPARE_CAP:
        ids = {p.get("document_id") for p in picked}
        for p in scored:
            if len(picked) >= PRODUCT_COMPARE_CAP:
                break
            if p.get("document_id") in ids:
                continue
            picked.append(p)
            ids.add(p.get("document_id"))
    return picked[:PRODUCT_COMPARE_CAP]


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
            "coverage_codes": list(getattr(rec, "coverage_codes", ()) or ()),
        })
        merged = dict(p)
        merged["name"] = labs["display_name"] or p.get("title") or ""
        merged["product_line"] = labs["product_line"]
        merged["product_line_label"] = labs["product_line_label"]
        merged["doc_kind_label"] = labs["doc_kind_label"]
        merged["cost_nature"] = labs["cost_nature"]
        merged["cost_nature_label"] = labs["cost_nature_label"]
        merged["insurer"] = labs["insurer"]
        merged["source_url"] = labs["source_url"]
        merged["summary"] = labs["summary"]
        merged["coverage_codes"] = list(labs.get("coverage_codes") or [])
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


def _format_count_ko(n) -> str:
    try:
        num = int(n)
    except (TypeError, ValueError):
        return str(n)
    if num >= 100_000_000:
        return f"{num / 100_000_000:.1f}".rstrip("0").rstrip(".") + "억"
    if num >= 10_000:
        man = num / 10_000
        if man >= 100 and abs(man - round(man)) < 0.05:
            return f"{int(round(man))}만"
        return f"{man:.1f}".rstrip("0").rstrip(".") + "만"
    return f"{num:,}"


def _format_share_pct(share) -> str:
    try:
        return f"{round(float(share) * 100)}%"
    except (TypeError, ValueError):
        return str(share)


def _format_kcd(kcd: str) -> str:
    return re.sub(r"\s+", "", str(kcd or ""))


def _claim_pattern_summary(
        ref_stats: list[dict] | None,
        age_band: str | None = None,
        sex: str | None = None) -> str:
    src = "건강보험심사평가원 질병정보서비스"
    rows = list(ref_stats or [])
    cached = bool(rows) and all(bool(s.get("cached", True)) for s in rows)
    src_label = f"{src}(저장본)" if cached else src
    age_lab = AGE_QUERY_LABELS.get(age_band or "")
    sex_lab = SEX_QUERY_LABELS.get(sex or "")
    cohort = ""
    if age_lab and sex_lab:
        cohort = f"같은 {age_lab} {sex_lab} 코호트의 "
    elif age_lab:
        cohort = f"같은 {age_lab} 코호트의 "
    elif sex_lab:
        cohort = f"같은 {sex_lab} 코호트의 "
    if not rows:
        return (
            f"{src} 기준 청구 규모·입원 비중 등 자세한 숫자는 "
            "아래 공공 참고 카드에서 확인해 주세요."
        )
    shares: list[float] = []
    for s in rows:
        try:
            if s.get("inpatient_share") is not None:
                shares.append(float(s["inpatient_share"]))
        except (TypeError, ValueError):
            continue
    if not shares:
        return (
            f"{src_label}의 {cohort}청구 참고 규모가 있으며, "
            "자세한 숫자는 아래 공공 참고 카드에서 확인해 주세요."
        )
    mx = max(shares)
    if mx <= 0.08:
        level = "대체로 외래 청구가 중심"
    elif mx <= 0.15:
        level = "외래 중심이며 일부는 입원 비중이 조금 더 있음"
    else:
        level = "외래와 입원을 함께 살펴볼 여지가 있음"
    return (
        f"{src_label}의 {cohort}청구 패턴을 보면 {level}입니다. "
        "건수·입원 비중은 아래 공공 참고 카드에 두었습니다."
    )


def _why_fallback(rules: RuleResult,
                  products: list[dict] | None = None,
                  ref_stats: list[dict] | None = None,
                  age_band: str | None = None,
                  sex: str | None = None,
                  symptom_clusters: list[dict] | None = None,
                  cluster_stats: list[dict] | None = None) -> str:
    """공공데이터 해석(3문단) · 주의·참고 · 비교 상품 2개 폴백."""
    usual_s, hidden_s = _symptom_type_labels(rules)
    kb = load_kb()
    specs = kb.get("symptoms") or {}
    name_bits: list[str] = []
    seen_names: set[str] = set()
    for h in rules.symptoms:
        for cond in ref_conditions_of(specs.get(h.code)):
            nm = str(cond.get("name") or "").strip()
            if not nm or nm in seen_names:
                continue
            seen_names.add(nm)
            name_bits.append(f"「{nm}」")
    area_labels = [a.label for a in rules.areas[:4]]
    fam_labels: list[str] = []
    for cl in symptom_clusters or []:
        for fam in cl.get("related_families") or []:
            lab = (fam.get("label") or "").strip()
            if lab and lab not in fam_labels:
                fam_labels.append(lab)
    lines: list[str] = ["## 공공데이터 해석"]
    p1 = (
        "질병관리청 국가건강정보포털 개요를 기준으로, "
        f"자주 느끼는 증상({usual_s})과 놓치기 쉬운 불편({hidden_s})은"
    )
    if name_bits:
        p1 += " 각각 " + ", ".join(name_bits) + " 등으로 연결됩니다."
    else:
        p1 += " 함께 묶어 살펴봅니다."
    if fam_labels:
        p1 += (
            " 이 조합은 "
            + ", ".join(fam_labels)
            + "을(를) 함께 점검할 때 쓰는 참고 축이며, "
            "특정 질환의 확진이 아닙니다."
        )
    elif area_labels:
        p1 += (
            " 이 조합은 "
            + ", ".join(area_labels)
            + " 영역을 함께 점검할 때 쓰는 참고 축이며, "
            "특정 질환의 확진이 아닙니다."
        )
    else:
        p1 += " 특정 질환의 확진이 아닙니다."
    lines.append(p1)
    stats_for_summary = list(cluster_stats or []) or list(ref_stats or [])
    lines.append(_claim_pattern_summary(
        stats_for_summary, age_band=age_band, sex=sex))

    cov_labels = [c.label for c in rules.coverages]
    occ_lab = OCCUPATIONS.get(rules.occupation or "")
    concern_labs = [CONCERNS[c] for c in rules.concerns] if rules.concerns else []
    age_lab = AGE_QUERY_LABELS.get(age_band or "")
    sex_lab = SEX_QUERY_LABELS.get(sex or "")
    p3_parts: list[str] = []
    who = " · ".join(x for x in (occ_lab, age_lab, sex_lab) if x)
    if who and concern_labs:
        p3_parts.append(
            f"{who} 맥락에서 "
            + ", ".join(concern_labs)
            + "을(를) 보시면"
        )
    elif who:
        p3_parts.append(f"{who} 맥락을 반영하면")
    elif concern_labs:
        p3_parts.append(
            ", ".join(concern_labs) + "을(를) 보시면"
        )
    if rules.occupation and (symptom_clusters or []):
        p3_parts.append(
            "직업별 질환 통계는 아직 연동되지 않아 "
            "보장 규칙 가중·맥락으로만 반영합니다"
        )
    if cov_labels:
        lead = (
            "원인 확인용 통원·검사 축과 입원 시 보장 축을 함께 보는 편이 "
            "실무적으로 맞습니다. "
            if any(c in ("inpatient", "surgery") for c in rules.concerns)
            else ""
        )
        if list(products or []):
            p3_parts.append(
                lead
                + "이에 맞춰 "
                + ", ".join(cov_labels)
                + " 축을 중심으로 아래 비교 상품 2개를 골랐습니다"
            )
        else:
            p3_parts.append(
                lead
                + "이에 맞춰 "
                + ", ".join(cov_labels)
                + " 축을 중심으로 보시면 됩니다"
            )
    if not p3_parts:
        p3_parts.append(
            "선택하신 불편과 연결된 보장 유형을 함께 보시면 됩니다"
        )
    # Join with ". " carefully
    p3 = ". ".join(p.rstrip(". ") for p in p3_parts) + "."
    lines.append(p3)

    lines.append("")
    lines.append("## 주의·참고")
    care = ""
    for cl in symptom_clusters or []:
        if cl.get("care_hint"):
            care = cl["care_hint"]
            break
    lines.append(
        "이 안내는 확진이 아니며, 특정 상품의 모집·권유가 아닙니다. "
        "자세한 공공 수치와 개요는 아래 카드를 참고해 주세요."
        + ((" " + care) if care else "")
    )

    lines.append("")
    lines.append("## 비교할 상품 2개")
    picked = list(products or [])[:PRODUCT_COMPARE_CAP]
    if not picked:
        lines.append(
            "지금은 비교할 공용 설명서가 없습니다. "
            "보장 유형만 참고해 주세요.")
    for p in picked:
        name = (p.get("name") or p.get("title") or "").strip()
        if not name:
            continue
        kinds = p.get("matched_coverages") or []
        if kinds:
            joined = ", ".join(str(k) for k in kinds)
            lines.append(
                f"「{name}」은 설명서에 {joined} 관련 담보가 있어 "
                "이번 증상·보장과 비교하기 좋습니다.")
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


def _parse_brief(text: str, fallback: str) -> str:
    raw = (text or "").replace(DISCLAIMER, "").strip()
    raw = _strip_doc_markers(raw)
    if not raw:
        return fallback
    if re.search(r"##\s*공공데이터\s*해석", raw):
        return raw
    if re.search(r"##\s*상황\s*종합", raw):
        # 구형 헤딩 → 신형으로 치환해 UI split과 맞춤
        return re.sub(
            r"##\s*상황\s*종합", "## 공공데이터 해석", raw, count=1)
    why_m = re.search(r"##\s*맞는 이유\s*(.*?)(?=##\s*알아둘 점|$)", raw, re.S)
    if why_m and why_m.group(1).strip():
        return why_m.group(1).strip()
    return raw


def _parse_why_caution(text: str, why_fb: str, caution_fb: str) -> tuple[str, str]:
    """하위 호환 — 브리프 파서로 본문만 반환."""
    return _parse_brief(text, why_fb), caution_fb


def _facts(rules: RuleResult, citations: list[str],
           products: list[dict] | None = None,
           ref_stats: list[dict] | None = None,
           age_band: str | None = None,
           sex: str | None = None,
           symptom_clusters: list[dict] | None = None,
           cluster_stats: list[dict] | None = None) -> str:
    return _template(
        rules, citations, products, ref_stats=ref_stats,
        age_band=age_band, sex=sex,
        symptom_clusters=symptom_clusters,
        cluster_stats=cluster_stats)


def _public_ref_fact_lines(
        rules: RuleResult,
        ref_stats: list[dict] | None,
        symptom_clusters: list[dict] | None = None,
        cluster_stats: list[dict] | None = None) -> list[str]:
    """A 필드 — 포맷된 숫자만 제공. confidence·개요 전문은 브리프에 쓰지 말 것."""
    lines = [
        "[공공의료 참고 A — 브리프에서는 요약만. "
        "개요 전문·confidence·원시 건수·전조·유병률·참고용 안내는 쓰지 말 것. "
        "브리프 본문에 KCD·(R53) 등 상병코드를 쓰지 말 것(한글 상병명만). "
        "숫자는 아래 포맷만 인용]"
    ]
    kb = load_kb()
    specs = kb.get("symptoms") or {}
    for h in rules.symptoms:
        for cond in ref_conditions_of(specs.get(h.code)):
            src = cond.get("source") or ""
            lines.append(
                f"- 증상 {h.label} → 참고 상병명 {cond['name']}"
                + (f", 출처: {src}" if src else "")
                + f" [내부매칭코드 {_format_kcd(cond['kcd'])} — 브리프에 쓰지 말 것]"
            )
        overview = portal_overview_of(specs.get(h.code))
        if overview:
            first = (overview["text"] or "").split(".")[0].strip()
            if first:
                lines.append(
                    f"- overview_hint ({h.label}): {first}."
                )
    for a in rules.areas:
        src = a.source or ""
        lines.append(
            f"- 가능 영역: {a.label}"
            + (f" (출처: {src})" if src else "")
        )
    if symptom_clusters:
        lines.append("[증상 묶음 참고 — 확진·전조·유병률 금지]")
        for cl in symptom_clusters:
            lines.append(
                f"- cluster: {cl.get('id')} ({cl.get('label')}); "
                f"matched={','.join(cl.get('matched_symptoms') or [])}"
            )
            for fam in cl.get("related_families") or []:
                lines.append(
                    f"  - family: {fam.get('label')}; "
                    f"kcds={','.join(fam.get('example_kcds') or [])}; "
                    f"blurb={fam.get('blurb') or ''}"
                )
            if cl.get("occupation_note"):
                lines.append(f"  - occupation_note: {cl['occupation_note']}")
            if cl.get("care_hint"):
                lines.append(f"  - care_hint: {cl['care_hint']}")
    stats_mix = list(cluster_stats or []) or list(ref_stats or [])
    lines.append("- 청구 패턴 요약: " + _claim_pattern_summary(stats_mix))
    for s in ref_stats or []:
        lines.append("- 청구 통계(증상카드): " + _stat_fact_bits(s))
    for s in cluster_stats or []:
        lines.append("- 청구 통계(묶음코호트): " + _stat_fact_bits(s))
    if len(lines) == 1:
        lines.append("- (해당 없음)")
    return lines


def _stat_fact_bits(s: dict) -> str:
    kcd = _format_kcd(s.get("kcd") or "")
    name = s.get("name") or ""
    bits = [f"KCD {kcd} {name}"]
    if s.get("total_count") is not None:
        bits.append(f"총 {_format_count_ko(s['total_count'])}건")
    if s.get("age_sex_count") is not None:
        bits.append(f"연령·성별 {_format_count_ko(s['age_sex_count'])}건")
    if s.get("inpatient_share") is not None:
        bits.append(f"입원비중 {_format_share_pct(s['inpatient_share'])}")
    bits.append("cached" if s.get("cached") else "live")
    if s.get("source"):
        bits.append(f"출처 {s['source']}")
    return "; ".join(bits)


def _template(rules: RuleResult, citations: list[str],
              products: list[dict] | None = None,
              ref_stats: list[dict] | None = None,
              age_band: str | None = None,
              sex: str | None = None,
              symptom_clusters: list[dict] | None = None,
              cluster_stats: list[dict] | None = None) -> str:
    usual_s, hidden_s = _symptom_type_labels(rules)
    lines = _public_ref_fact_lines(
        rules, ref_stats,
        symptom_clusters=symptom_clusters,
        cluster_stats=cluster_stats)
    lines.append("")
    lines.append("[보험 매칭 B]")
    lines.append("자주 느끼는 증상: " + usual_s)
    lines.append("놓치기 쉬운 불편: " + hidden_s)
    if rules.occupation:
        lines.append("직업군: " + OCCUPATIONS[rules.occupation])
        if symptom_clusters:
            lines.append(
                "직업별 질환 통계: 없음(맥락·규칙 가중만)"
            )
    if rules.concerns:
        lines.append("관심 보장: " + ", ".join(CONCERNS[c] for c in rules.concerns))
    age_lab = AGE_QUERY_LABELS.get(age_band or "")
    sex_lab = SEX_QUERY_LABELS.get(sex or "")
    if age_lab:
        lines.append("연령대: " + age_lab)
    if sex_lab:
        lines.append("성별: " + sex_lab)
    lines.append("")
    lines.append("살펴볼 보장 유형:")
    for c in rules.coverages:
        lines.append(f"- {c.label}: {c.summary}")
        lines.append("  이유: " + "; ".join(c.why))
    if products:
        lines.append("")
        lines.append("비교할 상품 2개(선정 목록, 가입 강요 아님):")
        for p in products[:PRODUCT_COMPARE_CAP]:
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
            cost = (p.get("cost_nature_label") or p.get("cost_nature") or "").strip()
            if cost:
                lines.append("  cost_nature: " + cost)
            if summary:
                lines.append("  summary: " + summary)
    if citations:
        lines.append("")
        lines.append("업로드한 약관에서 찾은 근거: " + "; ".join(citations))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
