"""치료 패턴·걱정 → 점검할 보장 순위 (결정론, LLM 금지)."""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from harag.guidance.engine import CONCERNS, OCCUPATIONS

logger = logging.getLogger("harag.coverage_priority")

RULES_PATH = Path(__file__).with_name("coverage_priority_rules.yaml")

FIELD_LIKE = frozenset({"field", "agriculture", "fishery"})
AXES_FALLBACK = frozenset(
    {"outpatient", "inpatient", "tests", "injury", "surgery"})
NATURES_FALLBACK = frozenset({"indemnity", "lump_sum", "either"})

# 가입 권유·확률 표현 금지 (quality gate)
_FORBIDDEN_REASON = re.compile(
    r"(가입하세요|가입을 추천|유병률|가능성이\s*\d|확률\s*\d)",
    re.I,
)


@lru_cache(maxsize=1)
def load_priority_rules() -> dict[str, Any]:
    if not RULES_PATH.is_file():
        return {}
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("coverage_priority_rules load fail: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def reload_priority_rules() -> dict[str, Any]:
    load_priority_rules.cache_clear()
    return load_priority_rules()


def validate_priority_rules(data: dict[str, Any] | None = None) -> list[str]:
    """스키마 오류 메시지 목록. 비어 있으면 OK."""
    raw = data if data is not None else load_priority_rules()
    errors: list[str] = []
    if not raw:
        return ["rules file empty or missing"]
    axes = set(raw.get("axes") or list(AXES_FALLBACK))
    natures = set(raw.get("natures") or list(NATURES_FALLBACK))
    rules = raw.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules must be a non-empty list")
        return errors
    seen_ids: set[str] = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"rules[{i}] not an object")
            continue
        rid = str(rule.get("id") or "").strip()
        if not rid:
            errors.append(f"rules[{i}] missing id")
        elif rid in seen_ids:
            errors.append(f"duplicate id: {rid}")
        else:
            seen_ids.add(rid)
        emit = rule.get("emit") or {}
        if not isinstance(emit, dict):
            errors.append(f"{rid or i}: emit missing")
            continue
        axis = str(emit.get("axis") or "")
        nature = str(emit.get("nature") or "")
        if axis not in axes:
            errors.append(f"{rid}: unknown axis {axis!r}")
        if nature not in natures:
            errors.append(f"{rid}: unknown nature {nature!r}")
        reason = str(emit.get("reason") or "")
        if not reason.strip():
            errors.append(f"{rid}: empty reason")
        elif _FORBIDDEN_REASON.search(reason):
            errors.append(f"{rid}: forbidden phrase in reason")
        try:
            float(emit.get("priority_weight") or 0)
        except (TypeError, ValueError):
            errors.append(f"{rid}: bad priority_weight")
    return errors


def derive_care_pattern(
        area_cards: list[dict] | None,
        thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    """area_cards severity.inpatient_share → care_pattern."""
    rules = load_priority_rules()
    th = thresholds or rules.get("thresholds") or {}
    high = float(th.get("inpatient_share_high") or 0.15)
    out_max = float(th.get("inpatient_share_outpatient_max") or 0.08)
    labels = rules.get("care_pattern_labels") or {}

    shares: list[float] = []
    for card in area_cards or []:
        sev = (card or {}).get("severity") or {}
        if sev.get("missing"):
            continue
        raw = sev.get("inpatient_share")
        if raw is None:
            continue
        try:
            shares.append(float(raw))
        except (TypeError, ValueError):
            continue

    if not shares:
        pattern = "unknown"
        share = None
    else:
        share = max(shares)
        if share >= high:
            pattern = "inpatient_heavy"
        elif share <= out_max:
            pattern = "outpatient_heavy"
        else:
            pattern = "mixed"

    return {
        "care_pattern": pattern,
        "care_pattern_label": labels.get(pattern) or pattern,
        "inpatient_share": share,
        "inpatient_share_pct": (
            round(100.0 * share, 1) if share is not None else None
        ),
    }


_COST_SELF_PAY_NOTE = (
    "아래 진료비는 건강보험 급여 기준 총진료비로 알려져 있으며, "
    "본인이 실제 내는 금액은 이보다 낮을 수 있고 비급여는 별도입니다. "
    "실손이 보상하는 범위는 상품요약서·약관에서 확인하세요."
)


def build_cost_pattern(
        area_cards: list[dict] | None = None,
        kosis: dict | None = None,
        concerns: list[str] | None = None) -> dict[str, Any]:
    """걱정(입원·수술) 전용 비용 블록 — care/수술은 여기 1회만."""
    care = derive_care_pattern(area_cards)
    lines: list[dict[str, Any]] = []
    missing_notes: list[str] = []
    concern_set = set(concerns or [])
    inpatientish = "inpatient" in concern_set

    title = (
        "걱정하신 입원·수술 비용"
        if inpatientish else "비용은 어떻게 나갈까요?"
    )

    if care["care_pattern"] == "unknown":
        missing_notes.append(
            "영역별 입원 비중 참고값이 없어 치료 패턴을 숫자로 못 붙였어요."
        )
    else:
        share_txt = (
            f"입원 비중 약 {care['inpatient_share_pct']:g}%"
            if care.get("inpatient_share_pct") is not None else ""
        )
        lines.append({
            "title": "참고 영역 치료 패턴",
            "text": (
                f"{care['care_pattern_label']}"
                + (f" · {share_txt}" if share_txt else "")
            ),
            "source": "건강보험심사평가원 질병정보서비스",
            "missing": False,
        })

    # 상위 펼침 카드(최대 2)의 심각도만 짧게
    for card in (area_cards or [])[:2]:
        sev = card.get("severity") or {}
        if sev.get("missing"):
            continue
        bits = [str(sev.get("label") or "").strip()]
        if sev.get("inpatient_share") is not None:
            try:
                pct = round(100.0 * float(sev["inpatient_share"]), 1)
                bits.append(f"입원 비중 약 {pct:g}%")
            except (TypeError, ValueError):
                pass
        lines.append({
            "title": str(card.get("title") or "영역"),
            "text": " · ".join(b for b in bits if b),
            "source": str(sev.get("source") or ""),
            "missing": False,
        })

    kosis = kosis or {}
    care_ctx = kosis.get("care_context") or {}
    if care_ctx and not care_ctx.get("missing") and care_ctx.get("label"):
        lines.append({
            "title": "연령·성별 전체 진료 이용",
            "text": str(care_ctx.get("detail") or care_ctx.get("label") or ""),
            "source": str(care_ctx.get("source") or ""),
            "missing": False,
        })
        if care_ctx.get("note"):
            missing_notes.append(str(care_ctx["note"]))
        inp = care_ctx.get("inpatient") or {}
        if inp and not inp.get("missing") and inp.get("label"):
            lines.append({
                "title": "연령·성별 입원 이용",
                "text": str(inp.get("detail") or inp.get("label") or ""),
                "source": str(inp.get("source") or ""),
                "missing": False,
            })

    surg = kosis.get("surgery_nhi") or {}
    if surg and not surg.get("missing") and surg.get("label"):
        lines.append({
            "title": "수술 참고(건강보험)",
            "text": str(surg.get("detail") or surg.get("label") or ""),
            "source": str(surg.get("source") or ""),
            "missing": False,
        })

    return {
        "title": title,
        "care_pattern": care.get("care_pattern") or "unknown",
        "care_pattern_label": care.get("care_pattern_label") or "",
        "inpatient_share_pct": care.get("inpatient_share_pct"),
        "lines": lines,
        "missing_notes": missing_notes,
        "note": (
            "특정 상병의 개인 예상 비용이 아니라 집단 통계 참고입니다. "
            + _COST_SELF_PAY_NOTE
        ),
    }


def _occupation_risk(occupation: str | None) -> str:
    occ = (occupation or "").strip()
    if occ in FIELD_LIKE:
        return "field_like"
    return "none"


def _when_match(
        when: dict[str, Any],
        concerns: set[str],
        care_pattern: str,
        occupation_risk: str) -> bool:
    if when.get("always_if_no_concern"):
        return not concerns

    concerns_any = [str(x) for x in (when.get("concerns_any") or []) if x]
    if concerns_any and not (set(concerns_any) & concerns):
        return False

    care_any = [str(x) for x in (when.get("care_pattern_any") or []) if x]
    if care_any and care_pattern not in care_any:
        return False

    occ_any = [str(x) for x in (when.get("occupation_risk_any") or []) if x]
    if occ_any and occupation_risk not in occ_any:
        return False

    # when이 실질 조건 없이 always만 아니면, 빈 when은 매칭하지 않음
    if not concerns_any and not care_any and not occ_any:
        return False
    return True


def _title_for(rules: dict, axis: str, nature: str) -> str:
    titles = rules.get("display_titles") or {}
    key = f"{axis}|{nature}"
    return str(titles.get(key) or f"{axis}/{nature}")


def _fill_reason(
        template: str,
        *,
        concern_label: str,
        care_pattern_label: str,
        inpatient_share_pct: float | None,
        occupation_label: str) -> str:
    share_clause = ""
    if inpatient_share_pct is not None:
        share_clause = (
            f" (참고 영역의 입원 비중 약 {inpatient_share_pct:g}%)"
        )
    text = template
    text = text.replace("{concern_label}", concern_label or "선택하신 걱정")
    text = text.replace("{care_pattern_label}", care_pattern_label or "")
    text = text.replace("{inpatient_share_clause}", share_clause)
    if inpatient_share_pct is not None:
        text = text.replace(
            "{inpatient_share_pct}", f"{inpatient_share_pct:g}")
    else:
        text = text.replace("{inpatient_share_pct}", "")
    text = text.replace("{occupation_label}", occupation_label or "")
    # 빈 절 정리
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"\s+\.", ".", text)
    return text


def build_coverage_priority(
        *,
        concerns: list[str] | None = None,
        occupation: str | None = None,
        area_cards: list[dict] | None = None,
        age_band: str | None = None,
        sex: str | None = None) -> dict[str, Any]:
    """점검할 보장 순위. age/sex는 향후 soft hint용으로만 받고 v1에서는 미사용."""
    del age_band, sex  # v1 unused — 가입 권유·연령 필터 금지
    rules_doc = load_priority_rules()
    if not rules_doc:
        return {
            "selection_basis": "",
            "care_pattern": "unknown",
            "care_pattern_label": "치료 패턴 미확인",
            "inpatient_share_pct": None,
            "items": [],
            "missing_notes": ["보장 순위 규칙표를 불러오지 못했어요."],
        }

    concern_list = [c for c in (concerns or []) if c in CONCERNS]
    concern_set = set(concern_list)
    care = derive_care_pattern(area_cards, rules_doc.get("thresholds"))
    care_pattern = care["care_pattern"]
    occ_risk = _occupation_risk(occupation)
    occ_label = OCCUPATIONS.get(occupation or "") or ""

    missing_notes: list[str] = []
    if not (occupation or "").strip():
        missing_notes.append(
            "직업 정보가 없어 직군 요인은 반영하지 않았어요."
        )
    elif occupation in ("student", "homemaker", "retired", "other"):
        missing_notes.append(
            f"{occ_label or '선택 직군'}이라 직군 상해 규칙은 적용하지 않았어요."
        )
    if care_pattern == "unknown":
        missing_notes.append(
            "치료 패턴 통계가 없어 이번 순위는 걱정(및 직군) 기준이에요."
        )

    primary_concern = concern_list[0] if concern_list else None
    concern_label = CONCERNS.get(primary_concern or "") or ""
    # 입원 걱정이면 라벨을 입원·수술로
    if "inpatient" in concern_set:
        concern_label = CONCERNS["inpatient"]

    candidates: list[dict[str, Any]] = []
    for rule in rules_doc.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        when = rule.get("when") or {}
        emit = rule.get("emit") or {}
        if not _when_match(when, concern_set, care_pattern, occ_risk):
            continue
        if emit.get("requires_stats") and care_pattern == "unknown":
            continue
        axis = str(emit.get("axis") or "")
        nature = str(emit.get("nature") or "")
        try:
            weight = float(emit.get("priority_weight") or 0)
        except (TypeError, ValueError):
            weight = 0.0
        reason = _fill_reason(
            str(emit.get("reason") or ""),
            concern_label=concern_label,
            care_pattern_label=str(care.get("care_pattern_label") or ""),
            inpatient_share_pct=care.get("inpatient_share_pct"),
            occupation_label=occ_label,
        )
        if _FORBIDDEN_REASON.search(reason):
            continue
        candidates.append({
            "id": str(rule.get("id") or ""),
            "axis": axis,
            "nature": nature,
            "priority_weight": weight,
            "title": _title_for(rules_doc, axis, nature),
            "reason": reason,
            "check_points": [
                str(x) for x in (emit.get("check_points") or []) if str(x).strip()
            ],
        })

    # dedupe by axis|nature — keep max weight
    best: dict[str, dict[str, Any]] = {}
    for c in candidates:
        key = f"{c['axis']}|{c['nature']}"
        prev = best.get(key)
        if prev is None or c["priority_weight"] > prev["priority_weight"]:
            best[key] = c
        elif (c["priority_weight"] == prev["priority_weight"]
              and c["id"] < prev["id"]):
            best[key] = c

    ranked = sorted(
        best.values(),
        key=lambda x: (-x["priority_weight"], x["id"]),
    )
    cap = int(rules_doc.get("cap") or 3)
    items = []
    for i, row in enumerate(ranked[:cap], start=1):
        items.append({
            "rank": i,
            "axis": row["axis"],
            "nature": row["nature"],
            "title": row["title"],
            "reason": row["reason"],
            "check_points": row["check_points"],
            "rule_id": row["id"],
        })

    basis = str(
        (rules_doc.get("meta") or {}).get("selection_basis_default")
        or "걱정 일치 > 치료 패턴 > 직군 상해"
    )
    return {
        "selection_basis": basis,
        "care_pattern": care_pattern,
        "care_pattern_label": care.get("care_pattern_label") or "",
        "inpatient_share_pct": care.get("inpatient_share_pct"),
        "items": items,
        "missing_notes": missing_notes,
    }
