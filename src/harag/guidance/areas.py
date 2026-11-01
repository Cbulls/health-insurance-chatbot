"""염두에 둘 영역 카드 조립 — 통계 라벨은 결정론, LLM 숫자 금지."""
from __future__ import annotations

from typing import Any

from harag.guidance.age_bands import AGE_UI_LABELS as _AGE_UI
from harag.guidance.engine import CONCERNS, OCCUPATIONS, CoverageHit, RuleResult
from harag.guidance.lexicon import load_kb
from harag.guidance.occupation_stats import (
    OccupationStatsClient,
    default_occupation_stats,
)
from harag.guidance.stat_labels import (
    commonness_from_counts,
    severity_label,
)

AREA_CARD_CAP = 4
AREA_CARD_PRIMARY = 2
_SEX_UI = {"male": "남성", "female": "여성"}

_USER_LIMIT_DROP = (
    "임상 후보 문구는 초안",
    "draft",
    "의료 검수가 필요",
    "진단이 아닙니다",
    "집단 청구 경향",
    "개인의 중증도",
)


def load_areas_catalog(kb: dict | None = None) -> dict[str, dict]:
    data = kb if kb is not None else load_kb()
    raw = data.get("areas_catalog") or {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _norm_kcd(kcd: str) -> str:
    return "".join(c for c in str(kcd or "").upper() if c.isalnum())


def _stats_by_kcd(ref_stats: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in ref_stats or []:
        key = _norm_kcd(row.get("kcd") or "")
        if key and key not in out:
            out[key] = row
    return out


def _symptom_area_degrees(catalog: dict[str, dict]) -> dict[str, int]:
    deg: dict[str, int] = {}
    for spec in catalog.values():
        for s in spec.get("symptom_codes") or []:
            code = str(s)
            if code:
                deg[code] = deg.get(code, 0) + 1
    return deg


def _specificity_score(
        linked: list[str], deg: dict[str, int]) -> float:
    """연결된 영역 수가 적은 증상일수록 가중↑."""
    if not linked:
        return 0.0
    score = 0.0
    for code in linked:
        n = max(int(deg.get(code) or 1), 1)
        score += 1.0 / float(n)
    score += 0.15 * len(linked)
    return score


def match_area_specs(symptom_codes: list[str],
                     catalog: dict[str, dict] | None = None
                     ) -> list[tuple[str, dict, list[str], float]]:
    """(area_id, spec, linked_codes, match_score) 특이도 점수 순."""
    codes = [c for c in symptom_codes if c]
    code_set = set(codes)
    cat = catalog if catalog is not None else load_areas_catalog()
    deg = _symptom_area_degrees(cat)
    hits: list[tuple[float, int, str, dict, list[str]]] = []
    for aid, spec in cat.items():
        want = [str(s) for s in (spec.get("symptom_codes") or []) if s]
        linked = [s for s in want if s in code_set]
        if not linked:
            continue
        score = _specificity_score(linked, deg)
        hits.append((score, len(linked), aid, spec, linked))
    hits.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [
        (aid, spec, linked, score)
        for score, _n, aid, spec, linked in hits[:AREA_CARD_CAP]
    ]


def scrub_user_limits(limits: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in limits or []:
        t = str(raw).strip()
        if not t:
            continue
        if any(x in t for x in _USER_LIMIT_DROP):
            continue
        out.append(t)
    return out


def scrub_stat_reason(reason: str | None) -> str:
    """UI용 — 캐시·KCD 코드 등 내부 표현 순화."""
    text = str(reason or "").strip()
    if not text:
        return ""
    if "캐시 미적재" in text or "심평원 캐시" in text:
        return "이 영역의 청구 참고값을 아직 준비하지 못했어요."
    if "캐시" in text:
        return "참고 통계를 아직 준비하지 못했어요."
    return text


def _rep_kcd(kcds: list[str], by_kcd: dict[str, dict]) -> str | None:
    best = None
    best_n = -1
    for raw in kcds:
        key = _norm_kcd(raw)
        if not key:
            continue
        row = by_kcd.get(key)
        n = None
        if row and row.get("age_sex_count") is not None:
            try:
                n = int(row["age_sex_count"])
            except (TypeError, ValueError):
                n = None
        if n is not None and n > best_n:
            best_n = n
            best = key
        elif best is None:
            best = key
    return best


def build_area_cards(
        rules: RuleResult,
        ref_stats: list[dict] | None = None,
        age_band: str | None = None,
        sex: str | None = None,
        occupation_client: OccupationStatsClient | None = None,
        kb: dict | None = None) -> list[dict]:
    data = kb if kb is not None else load_kb()
    catalog = load_areas_catalog(data)
    specs = data.get("symptoms") or {}
    code_to_label = {
        h.code: (h.label or h.code)
        for h in rules.symptoms
    }
    # 라벨 보강
    for code, spec in specs.items():
        if code not in code_to_label and isinstance(spec, dict):
            code_to_label[code] = str(spec.get("label") or code)

    selected_codes = [h.code for h in rules.symptoms]
    matched = match_area_specs(selected_codes, catalog)
    if not matched:
        return []

    by_kcd = _stats_by_kcd(ref_stats)
    # 세션 닫힌 집합: 매칭된 영역들의 모든 KCD
    session_counts: list[tuple[str, int | None]] = []
    seen_k: set[str] = set()
    for _aid, spec, _linked, _sc in matched:
        for raw in spec.get("kcds") or []:
            key = _norm_kcd(raw)
            if not key or key in seen_k:
                continue
            seen_k.add(key)
            row = by_kcd.get(key)
            session_counts.append(
                (key, None if not row else row.get("age_sex_count")))

    age_lab = _AGE_UI.get(age_band or "") or (age_band or "")
    sex_lab = _SEX_UI.get(sex or "") or ""
    occ_client = occupation_client or default_occupation_stats()
    selected_n = len(selected_codes)

    cards: list[dict] = []
    for aid, spec, linked, match_score in matched:
        kcds = [_norm_kcd(k) for k in (spec.get("kcds") or []) if _norm_kcd(k)]
        focus = _rep_kcd(kcds, by_kcd)
        common = commonness_from_counts(
            session_counts, focus_kcd=focus,
            age_band=age_lab, sex=sex_lab)
        row = by_kcd.get(focus or "") if focus else None
        sev = severity_label(
            None if not row else row.get("inpatient_share"))
        if row and not sev.get("missing"):
            src = str(row.get("source") or sev.get("source") or "")
            sev["source"] = src
            if row.get("note"):
                sev["note"] = str(row["note"])
        elif row and row.get("source"):
            sev["source"] = str(row.get("source") or "")

        occ = occ_client.lookup(aid, rules.occupation)
        # HIRA 캐시 미적재 고지 (guide 경로에서 OpenAPI 호출 없음)
        from harag.guidance.hira import load_cache
        uncached = [k for k in kcds if load_cache(k) is None]
        if common.get("missing") and uncached:
            common = {
                **common,
                "reason": scrub_stat_reason(
                    "심평원 캐시 미적재 (" + ", ".join(uncached) + ")"
                ),
            }
        elif common.get("missing"):
            common = {
                **common,
                "reason": scrub_stat_reason(
                    common.get("reason")
                    or "닫힌 집합에 비교할 청구 건수가 부족합니다."
                ),
            }
        if sev.get("missing") and uncached and focus in uncached:
            sev = {
                **sev,
                "reason": scrub_stat_reason(f"심평원 캐시 미적재 ({focus})"),
            }
        elif sev.get("missing"):
            sev = {
                **sev,
                "reason": scrub_stat_reason(
                    sev.get("reason")
                    or "입원 비중 참고값이 없거나 해당 구간이 비어 있습니다."
                ),
            }

        groups = []
        for g in spec.get("candidate_groups") or []:
            if not isinstance(g, dict):
                continue
            lab = str(g.get("label") or "").strip()
            if not lab:
                continue
            groups.append({
                "label": lab,
                "source": str(g.get("source") or "").strip(),
            })
        cards.append({
            "id": aid,
            "title": str(spec.get("title") or aid),
            "clinical_review": str(spec.get("clinical_review") or ""),
            "linked_symptoms": [
                code_to_label.get(c, c) for c in linked
            ],
            "linked_count": len(linked),
            "selected_symptom_count": selected_n,
            "match_score": round(float(match_score), 4),
            "candidate_groups": groups,
            "kcds": kcds,
            "commonness": common,
            "severity": sev,
            "occupation_factor": {
                "text": occ.text,
                "source": occ.source,
                "missing": occ.missing,
                "claims_not_incidence": occ.claims_not_incidence,
                "reason": scrub_stat_reason(occ.reason or ""),
            },
            "limits": scrub_user_limits(
                [str(x) for x in (spec.get("limits") or [])]
            ),
        })
    return cards


def build_insurance_bridge(
        cards: list[dict],
        coverages: list[CoverageHit] | list[dict],
        occupation: str | None = None,
        concerns: list[str] | None = None,
        kosis: dict | None = None) -> dict[str, Any]:
    """영역 severity 집계 + 보장 축 → 상품명 없는 문장. KOSIS는 보조만."""
    codes: set[str] = set()
    labels: list[str] = []
    for c in coverages or []:
        if isinstance(c, dict):
            codes.add(str(c.get("code") or ""))
            lab = str(c.get("label") or "").strip()
        else:
            codes.add(getattr(c, "code", "") or "")
            lab = (getattr(c, "label", None) or "").strip()
        if lab and lab not in labels:
            labels.append(lab)

    outpatientish = 0
    inpatientish = 0
    for card in cards or []:
        sev = (card.get("severity") or {})
        if sev.get("missing"):
            continue
        lab = str(sev.get("label") or "")
        if "입원 비중을 함께" in lab:
            inpatientish += 1
        else:
            outpatientish += 1

    parts: list[str] = []
    soft: list[str] = []
    if "outpatient" in codes:
        soft.append("실손 통원")
    if "tests" in codes:
        soft.append("검사")
    if outpatientish >= 1 or soft:
        joined = "·".join(soft) if soft else "실손 통원"
        if inpatientish >= 1 or "inpatient" in codes:
            parts.append(
                f"외래 중심 영역이 보이니 {joined}(필요 시 입원) 한도를 "
                "먼저 확인해 보세요."
            )
        else:
            parts.append(
                f"외래 중심 영역이 보이니 {joined} 한도를 먼저 확인해 보세요."
            )
    elif "inpatient" in codes:
        parts.append("입원 보장을 중심으로 살펴볼 수 있습니다.")

    if occupation == "field" and "injury" in codes:
        parts.append("현장·운전·배달이면 상해 관련 실손 축도 함께 둡니다.")
    elif occupation == "agriculture" and (
            "injury" in codes or "outpatient" in codes):
        parts.append(
            "농업 업무면 통원·상해 축을 함께 점검해 볼 수 있습니다."
        )
    elif occupation == "fishery" and (
            "injury" in codes or "outpatient" in codes):
        parts.append(
            "어업 업무면 통원·상해 축을 함께 점검해 볼 수 있습니다."
        )
    elif occupation == "care_health" and (
            "injury" in codes or "outpatient" in codes):
        parts.append(
            "의료·돌봄 업무면 통원·상해 축을 함께 점검해 볼 수 있습니다."
        )
    elif "injury" in codes:
        parts.append("상해 관련 실손도 함께 둘 수 있습니다.")

    concern_set = set(concerns or [])
    if "tests" in concern_set or "tests" in codes:
        parts.append(
            "장기 치료 걱정이 있으면 정액 보장 필요 여부를 "
            "상담에서 살펴보세요."
        )

    kosis = kosis or {}
    surg = kosis.get("surgery_nhi") or {}
    if not surg.get("missing") and surg.get("label"):
        avg = None
        for s in surg.get("surgeries") or []:
            if s.get("avg_inpatient_days") is not None:
                avg = s["avg_inpatient_days"]
                break
        if avg is not None and avg > 7:
            parts.append(
                "건강보험 수술 통계상 관련 수술의 평균 입원이 긴 편이니 "
                "실손 입원·수술비 한도도 점검해 보세요."
            )
        elif avg is not None and avg > 3:
            parts.append(
                "관련 수술의 평균 입원일이 있어 입원 보장 필요 여부를 "
                "함께 보면 좋습니다."
            )

    cost = kosis.get("cost_bracket") or {}
    if not cost.get("missing") and cost.get("label"):
        share = cost.get("high_share_pct")
        if isinstance(share, (int, float)) and share >= 15:
            parts.append(
                "같은 연령대 입원 통계에서 고액 구간 비중이 있어 "
                "실손 입원 한도를 함께 점검해 보세요."
            )
        elif isinstance(share, (int, float)) and share >= 5:
            parts.append(
                "입원 진료비 구간 통계를 참고해 입원 보장 필요 여부를 "
                "상담에서 살펴볼 수 있습니다."
            )

    if not parts:
        if labels:
            parts.append(
                f"{', '.join(labels)} 보장 유형을 중심으로 살펴볼 수 있습니다."
            )
        else:
            parts.append(
                "선택하신 불편과 연결된 보장 유형을 중심으로 "
                "살펴볼 수 있습니다."
            )

    occ_lab = OCCUPATIONS.get(occupation or "")
    return {
        "paragraphs": parts,
        "occupation_label": occ_lab or "",
        "concern_labels": [CONCERNS[c] for c in (concerns or []) if c in CONCERNS],
        "coverage_labels": labels,
        "kosis_care": kosis.get("care_context") or {},
        "kosis_surgery_nhi": surg,
        "kosis_surgery_medical_aid": kosis.get("surgery_medical_aid") or {},
        "kosis_cost_bracket": cost,
        "kosis_source": kosis.get("source") or "",
    }
