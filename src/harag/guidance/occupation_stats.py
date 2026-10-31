"""직군·업무상질병 통계 — KOSIS 고용노동부 캐시/stat_fact 조회.

산재 승인(발생현황) 건수이며 개인 발생률이 아님.
업종 분류는 직군 칩과 근사 연결(occupation_crosswalk.yaml).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

MISSING_TEXT = "찾지 못했어요"
TBL_N034 = "DT_11806_N034"
SOURCE_MOL = "KOSIS · 고용노동부"
CROSSWALK_PATH = Path(__file__).with_name("occupation_crosswalk.yaml")


@dataclass(frozen=True)
class OccupationEvidence:
    text: str
    source: str | None = None
    missing: bool = False
    # 산재 처리 건수 등일 때 True — UI에 발생률 아님 고지
    claims_not_incidence: bool = False
    reason: str = ""


class OccupationStatsClient(Protocol):
    def lookup(self, area_id: str, occupation: str | None) -> OccupationEvidence:
        ...


def _load_crosswalk() -> dict:
    if not CROSSWALK_PATH.is_file():
        return {}
    try:
        data = json.loads(CROSSWALK_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("occupations") or {}


class MolKosisOccupationStats:
    """DT_11806_N034 — 업종×규모. 규모=전체만 사용 (규모 입력 없음)."""

    def __init__(self, crosswalk: dict | None = None):
        self._cw = crosswalk if crosswalk is not None else _load_crosswalk()

    def lookup(self, area_id: str, occupation: str | None) -> OccupationEvidence:
        del area_id  # 영역별 질병유형 차원이 표에 없음
        occ = (occupation or "").strip()
        spec = self._cw.get(occ) if occ else None
        if not spec or str(spec.get("match") or "") == "none":
            from harag.guidance.engine import OCCUPATIONS
            lab = OCCUPATIONS.get(occ) or occ or "선택 직군"
            note = str((spec or {}).get("note") or "").strip()
            reason = f"이 직군({lab})은 산재 업종 통계와 연결하지 않음"
            if note:
                reason = f"{reason}. {note}"
            return OccupationEvidence(
                text=MISSING_TEXT, source=None, missing=True, reason=reason)

        industries = [str(x) for x in (spec.get("industries") or []) if x]
        preferred = str(spec.get("preferred") or (industries[0] if industries else ""))
        if not preferred:
            return OccupationEvidence(
                text=MISSING_TEXT, source=None, missing=True,
                reason="업종 매핑이 비어 있습니다.")

        from harag.guidance.stat_store import get_stat

        ind_stat = get_stat(
            TBL_N034, "count",
            {"industry": preferred, "size": "전체"},
            period="latest",
        )
        # preferred 없으면 매핑 목록에서 첫 가용값
        if ind_stat is None:
            for name in industries:
                ind_stat = get_stat(
                    TBL_N034, "count",
                    {"industry": name, "size": "전체"},
                    period="latest",
                )
                if ind_stat is not None:
                    preferred = name
                    break
        if ind_stat is None:
            return OccupationEvidence(
                text=MISSING_TEXT, source=None, missing=True,
                reason=f"업종 「{preferred}」 캐시 값이 없습니다.")

        total = get_stat(
            TBL_N034, "count",
            {"industry": "총계", "size": "전체"},
            period=ind_stat.period,
        )
        share = None
        if total and total.value > 0:
            share = 100.0 * float(ind_stat.value) / float(total.value)

        match = str(spec.get("match") or "approx")
        approx = " · 근사 연결" if match == "approx" else ""
        note = str(spec.get("note") or "").strip()
        share_txt = (
            f", 전체 업무상질병 승인 대비 약 {share:.1f}%"
            if share is not None else ""
        )
        text = (
            f"{preferred} 종사자(업종 통계) 기준으로, "
            f"업무상질병 승인(발생현황)은 "
            f"{int(round(ind_stat.value)):,}명{share_txt}입니다"
            f"{approx}."
        )
        if note:
            text = f"{text} {note}"
        src = (
            f"{SOURCE_MOL}, {ind_stat.table_name or TBL_N034}, "
            f"{ind_stat.period} · 규모 전체"
        )
        return OccupationEvidence(
            text=text,
            source=src,
            missing=False,
            claims_not_incidence=True,
        )


class NullOccupationStats:
    """근거 없음 — 숨기지 않고 찾지 못했어요."""

    def lookup(self, area_id: str, occupation: str | None) -> OccupationEvidence:
        return OccupationEvidence(text=MISSING_TEXT, source=None, missing=True)


def default_occupation_stats() -> OccupationStatsClient:
    """캐시/stat_fact가 있으면 MOL 조회, 없으면 Null."""
    from harag.guidance.kosis_nhis import load_table_cache
    from harag.guidance.stat_store import get_stat, load_all_caches_into_db

    cache = load_table_cache(TBL_N034)
    if cache:
        # 스냅샷이 있는데 DB가 비었을 수 있음 → lazy load
        probe = get_stat(
            TBL_N034, "count",
            {"industry": "총계", "size": "전체"},
            period="latest",
        )
        if probe is None:
            try:
                load_all_caches_into_db()
            except Exception as e:  # noqa: BLE001
                logger.warning("occupation stats db load failed: %s", e)
        return MolKosisOccupationStats()
    return NullOccupationStats()
