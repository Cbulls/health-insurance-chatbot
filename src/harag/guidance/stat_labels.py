"""닫힌 KCD 집합 기준 흔함·심각도(대리지표) 라벨."""
from __future__ import annotations

from typing import Any

# 닫힌 집합 백분위: 상위 20% / 중간 50% / 하위 30%
COMMONNESS_TOP_PCT = 0.20
COMMONNESS_MID_PCT = 0.70  # top+mid = 70%, remainder = rare

SEVERITY_OUTPATIENT_MAX = 0.08
SEVERITY_LOW_INPATIENT_MAX = 0.15

MISSING = "찾지 못했어요"


def severity_label(inpatient_share: float | None) -> dict[str, Any]:
    """입원 비중 → 심각도 참고 라벨. 평균 내원일수는 Phase 1에서 null."""
    if inpatient_share is None:
        return {
            "label": MISSING,
            "inpatient_share": None,
            "avg_visit_days": None,
            "source": "",
            "missing": True,
        }
    try:
        share = float(inpatient_share)
    except (TypeError, ValueError):
        return {
            "label": MISSING,
            "inpatient_share": None,
            "avg_visit_days": None,
            "source": "",
            "missing": True,
        }
    if share <= SEVERITY_OUTPATIENT_MAX:
        label = "대부분 외래 중심"
    elif share <= SEVERITY_LOW_INPATIENT_MAX:
        label = "외래 위주이며 입원 비중은 낮은 편"
    else:
        label = "입원 비중을 함께 볼 필요"
    return {
        "label": label,
        "inpatient_share": share,
        "avg_visit_days": None,
        "source": "건강보험심사평가원 질병정보서비스",
        "missing": False,
    }


def commonness_from_counts(
        counts: list[tuple[str, int | None]],
        focus_kcd: str | None = None,
        age_band: str | None = None,
        sex: str | None = None) -> dict[str, Any]:
    """닫힌 집합 안 상대 순위 → 흔한 편/보통/드문 편.

    counts: [(kcd, age_sex_count), ...]
    focus_kcd: 이 카드 대표 코드. 없으면 최대 count 코드.
    """
    scored: list[tuple[str, int]] = []
    for kcd, raw in counts:
        key = str(kcd or "").strip()
        if not key:
            continue
        if raw is None:
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n < 0:
            continue
        scored.append((key, n))
    if len(scored) < 2:
        return {
            "label": MISSING,
            "rank": None,
            "n": len(scored),
            "detail": "",
            "source": "",
            "missing": True,
        }
    # 동점이면 kcd 문자열로 안정 정렬
    scored.sort(key=lambda x: (-x[1], x[0]))
    n = len(scored)
    focus = (focus_kcd or "").strip()
    if not focus or focus not in {k for k, _ in scored}:
        focus = scored[0][0]
    rank = next(i + 1 for i, (k, _) in enumerate(scored) if k == focus)
    # 백분위: rank 1 = 가장 흔함 → pct near 0
    pct = (rank - 1) / max(n - 1, 1)
    if pct <= COMMONNESS_TOP_PCT:
        label = "흔한 편"
    elif pct <= COMMONNESS_MID_PCT:
        label = "보통"
    else:
        label = "드문 편"
    who = " · ".join(x for x in (age_band or "", sex or "") if x).strip()
    detail = (
        f"참고 질환군 {n}개 안 {rank}/{n}"
        + (f" ({who})" if who else "")
    )
    return {
        "label": label,
        "rank": rank,
        "n": n,
        "detail": detail,
        "source": "건강보험심사평가원 질병정보서비스",
        "missing": False,
    }
