"""결정론 공공통계 패널·추천 이유 — LLM은 윤문만, 숫자·보장 창작 금지."""
from __future__ import annotations

from typing import Any

from harag.guidance.engine import CONCERNS, OCCUPATIONS, CoverageHit, RuleResult
from harag.guidance.occupation_stats import _load_crosswalk


def _ctx_line(title: str, block: dict | None) -> dict[str, Any]:
    b = block or {}
    missing = bool(b.get("missing")) or (b.get("label") == "찾지 못했어요")
    reason = str(b.get("reason") or b.get("detail") or "").strip()
    if missing and not reason:
        reason = "캐시에 해당 구간이 없거나 연령·성별이 비어 있습니다."
    return {
        "key": title,
        "title": title,
        "label": str(b.get("label") or "찾지 못했어요"),
        "detail": str(b.get("detail") or ""),
        "source": str(b.get("source") or ""),
        "year": b.get("year"),
        "missing": missing,
        "reason": reason if missing else "",
        "note": str(b.get("note") or ""),
    }


def build_public_stats_panel(
        kosis: dict | None,
        area_cards: list[dict] | None,
        occupation: str | None = None,
        age_band: str | None = None,
        sex: str | None = None) -> dict[str, Any]:
    """결과 화면 전용 「공공 통계 참고」— 접지 않는 요약."""
    kosis = kosis or {}
    cards = area_cards or []
    items: list[dict[str, Any]] = []

    care = kosis.get("care_context") or {}
    care_item = _ctx_line("연령·성별 진료 이용(A043)", care)
    if not age_band or not sex or sex == "unspecified":
        if care_item["missing"]:
            care_item["reason"] = "연령·성별을 선택하면 진료 이용 통계를 붙입니다."
    items.append(care_item)

    inpatient = (care.get("inpatient") if isinstance(care, dict) else None) or {}
    if inpatient:
        items.append(_ctx_line("연령·성별 입원 이용(A047)", inpatient))
    else:
        items.append({
            "key": "연령·성별 입원 이용(A047)",
            "title": "연령·성별 입원 이용(A047)",
            "label": "찾지 못했어요",
            "detail": "",
            "source": "",
            "year": None,
            "missing": True,
            "reason": (
                "연령·성별을 선택하면 입원 이용 통계를 붙입니다."
                if not age_band or not sex or sex == "unspecified"
                else "입원 이용 캐시가 없거나 해당 구간이 비어 있습니다."
            ),
            "note": "",
        })

    # 직군 N034 — 영역 카드와 동일 조회 결과(첫 카드) 또는 crosswalk 고지
    occ_block: dict[str, Any] = {}
    for card in cards:
        of = card.get("occupation_factor") or {}
        if of:
            occ_block = of
            break
    occ_lab = OCCUPATIONS.get(occupation or "") or (occupation or "")
    cw = _load_crosswalk().get(occupation or "") if occupation else None
    if occ_block:
        missing = bool(occ_block.get("missing"))
        reason = str(occ_block.get("reason") or "").strip()
        if missing and not reason and cw and str(cw.get("match")) == "none":
            reason = (
                f"이 직군({occ_lab or '선택'})은 산재 업종 통계와 연결하지 않음"
            )
        elif missing and not reason:
            reason = "업종 캐시에 연결값이 없습니다."
        items.append({
            "key": "직군 산재(N034)",
            "title": "직군 산재(N034)",
            "label": (
                "찾지 못했어요" if missing
                else str(occ_block.get("text") or "")[:120]
            ),
            "detail": "" if missing else str(occ_block.get("text") or ""),
            "source": str(occ_block.get("source") or ""),
            "year": None,
            "missing": missing,
            "reason": reason if missing else "",
            "note": (
                "산재 처리 건수 기준이며 발생률은 아닙니다."
                if occ_block.get("claims_not_incidence") else ""
            ),
        })
    elif occupation and cw and str(cw.get("match")) == "none":
        items.append({
            "key": "직군 산재(N034)",
            "title": "직군 산재(N034)",
            "label": "찾지 못했어요",
            "detail": "",
            "source": "",
            "year": None,
            "missing": True,
            "reason": f"이 직군({occ_lab})은 산재 업종 통계와 연결하지 않음",
            "note": str(cw.get("note") or ""),
        })
    elif occupation:
        items.append({
            "key": "직군 산재(N034)",
            "title": "직군 산재(N034)",
            "label": "찾지 못했어요",
            "detail": "",
            "source": "",
            "year": None,
            "missing": True,
            "reason": "영역 카드가 없거나 업종 캐시에 연결값이 없습니다.",
            "note": "",
        })

    # 질병298·수술 — 영역과 연결될 때만 한 줄
    for card in cards:
        d298 = card.get("disease_298") or {}
        if d298 and not d298.get("missing") and d298.get("label"):
            items.append({
                "key": f"질병분류298:{card.get('id')}",
                "title": f"질병분류 급여(298) · {card.get('title') or ''}",
                "label": str(d298.get("label") or ""),
                "detail": str(d298.get("detail") or ""),
                "source": str(d298.get("source") or ""),
                "year": d298.get("year"),
                "missing": False,
                "reason": "",
                "note": str(d298.get("note") or ""),
            })
            break
    else:
        # 영역은 있으나 전부 missing이면 이유 한 줄
        miss_reasons = []
        for card in cards:
            d298 = card.get("disease_298") or {}
            if d298.get("missing"):
                r = str(d298.get("reason") or d298.get("detail") or "").strip()
                if r:
                    miss_reasons.append(r)
        if cards and miss_reasons:
            items.append({
                "key": "질병분류298",
                "title": "질병분류 급여(298)",
                "label": "찾지 못했어요",
                "detail": "",
                "source": "",
                "year": None,
                "missing": True,
                "reason": miss_reasons[0],
                "note": "",
            })

    for card in cards:
        surg = card.get("surgery_context") or {}
        if surg and not surg.get("missing") and surg.get("label"):
            items.append({
                "key": f"수술:{card.get('id')}",
                "title": f"수술 참고 · {card.get('title') or ''}",
                "label": str(surg.get("label") or ""),
                "detail": str(surg.get("detail") or ""),
                "source": str(surg.get("source") or ""),
                "year": surg.get("year"),
                "missing": False,
                "reason": "",
                "note": str(surg.get("note") or ""),
            })
            break

    # 영역별 HIRA 캐시 공백 고지
    area_notes: list[str] = []
    for card in cards:
        common = card.get("commonness") or {}
        sev = card.get("severity") or {}
        for block, kind in ((common, "흔함"), (sev, "심각도")):
            if block.get("missing"):
                r = str(block.get("reason") or "").strip()
                if r:
                    area_notes.append(
                        f"{card.get('title') or card.get('id')} {kind}: {r}"
                    )
    # 중복 제거
    seen: set[str] = set()
    notes: list[str] = []
    for n in area_notes:
        if n not in seen:
            seen.add(n)
            notes.append(n)

    lines = []
    for it in items:
        if it.get("missing"):
            lines.append(
                f"{it['title']}: 찾지 못했어요"
                + (f" ({it['reason']})" if it.get("reason") else "")
            )
        else:
            bit = it.get("detail") or it.get("label") or ""
            lines.append(f"{it['title']}: {bit}")

    return {
        "title": "공공 통계 참고",
        "items": items,
        "notes": notes,
        "summary_lines": lines,
    }


def build_recommendation_rationale(
        rules: RuleResult,
        products: list[dict] | None = None,
        public_stats: dict | None = None) -> dict[str, Any]:
    """증상 → 보장 → 상품 매칭을 고정 템플릿으로."""
    steps: list[str] = []
    missing_notes: list[str] = []

    cov_by_code = {c.code: c for c in rules.coverages}
    # 1. 증상별 보장
    for h in rules.symptoms:
        hit_labels = []
        for c in rules.coverages:
            for reason in c.why or []:
                if h.label in reason or f"'{h.label}'" in reason:
                    hit_labels.append(c.label)
                    break
        # 증상 기본 매칭이 why에 없으면 coverages 전체 중 상위 2개 약식
        if not hit_labels:
            hit_labels = [c.label for c in rules.coverages[:2]]
        # 중복 제거 유지 순서
        seen_l: list[str] = []
        for lab in hit_labels:
            if lab not in seen_l:
                seen_l.append(lab)
        if seen_l:
            steps.append(
                f"「{h.label}」→ 참고 보장 {', '.join(seen_l)} (KB 규칙)"
            )
        else:
            steps.append(f"「{h.label}」→ 연결된 보장 규칙이 없습니다")

    # 2. 걱정
    for con in rules.concerns:
        lab = CONCERNS.get(con) or con
        if con == "inpatient":
            steps.append(
                f"「{lab}」걱정 → 실손 입원 축을 함께 가중합니다"
            )
        elif con == "outpatient":
            steps.append(
                f"「{lab}」걱정 → 실손 통원 축을 함께 가중합니다"
            )
        elif con == "tests":
            steps.append(
                f"「{lab}」걱정 → 검사·장기 치료 관련 축을 함께 가중합니다"
            )
        else:
            steps.append(f"「{lab}」걱정 → 관련 보장 축을 함께 둡니다")

    # 3. 상품
    prods = products or []
    if prods:
        for p in prods[:2]:
            name = (p.get("name") or p.get("title") or "상품").strip()
            mats = [str(x) for x in (p.get("matched_coverages") or []) if x]
            if mats:
                steps.append(f"「{name}」: 매칭 담보 {', '.join(mats)}")
            else:
                steps.append(f"「{name}」: 매칭 담보 정보가 없습니다")
    else:
        steps.append("비교 상품이 없어 보장 유형만 연결합니다")

    # 4. 통계
    stats_used = False
    for it in (public_stats or {}).get("items") or []:
        if not it.get("missing") and (it.get("detail") or it.get("label")):
            bit = it.get("detail") or it.get("label")
            steps.append(f"참고한 공공 수치: {it.get('title')} — {bit}")
            stats_used = True
            break
    if not stats_used:
        steps.append(
            "이 추천은 통계 수치가 아니라 보장 유형 매칭을 중심으로 합니다"
        )

    # missing notes
    occ = rules.occupation
    cw = _load_crosswalk().get(occ or "") if occ else None
    if cw and str(cw.get("match")) == "none":
        lab = OCCUPATIONS.get(occ or "") or occ
        missing_notes.append(
            f"{lab} 직군은 산재 업종 통계와 연결하지 않습니다."
        )
    for it in (public_stats or {}).get("items") or []:
        if it.get("missing") and it.get("reason"):
            note = f"{it.get('title')}: {it['reason']}"
            if note not in missing_notes:
                missing_notes.append(note)
    for n in (public_stats or {}).get("notes") or []:
        if n not in missing_notes:
            missing_notes.append(n)

    # unused cov_by_code keeps mypy quiet if we later expand
    del cov_by_code

    return {
        "principle": "진단 아님 · 상품 권유 아님 · 가능 영역/보장 유형 연결",
        "steps": steps,
        "missing_notes": missing_notes,
    }


def public_stats_fact_lines(panel: dict | None) -> list[str]:
    """_facts에 넣을 한 줄들."""
    if not panel:
        return []
    lines = ["[공공통계: 결과 패널과 동일 문자열 — 숫자 창작 금지]"]
    for s in panel.get("summary_lines") or []:
        lines.append(f"- 공공통계: {s}")
    for n in panel.get("notes") or []:
        lines.append(f"- 공공통계고지: {n}")
    return lines
