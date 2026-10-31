"""KOSIS · 국민건강보험공단 통계 — OpenAPI는 배치 동기화만, guide()는 캐시만 읽는다."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harag.guidance.age_bands import age_ui_label, kosis_ages

logger = logging.getLogger("harag.kosis_nhis")

SOURCE = "KOSIS · 국민건강보험공단"
SOURCE_MOL = "KOSIS · 고용노동부"
CACHE_DIR = Path(__file__).with_name("kosis_cache")
OPENAPI_URL = (
    "https://kosis.kr/openapi/Param/statisticsParameterData.do"
)
TIMEOUT_S = 60

TBL_A043 = "TX_35001_A043"  # 연령별 성별 진료현황(총계)
TBL_A047 = "TX_35001_A047"  # 연령별 성별 진료현황(입원)
TBL_A054 = "TX_35001_A054"  # 연령별 진료비 크기별(입원)
TBL_A001 = "TX_35004_A001"  # 수술 현황(건강보험)
TBL_A002 = "TX_35004_A002"  # 수술 현황(의료급여)
TBL_N034 = "DT_11806_N034"  # 업무상질병 발생현황-규모별 (고용노동부)
TBL_A0611 = "DT_35001_A0611"  # 298 질병분류별 연령별 급여현황

ORG_NHIS = "350"
ORG_MOL = "118"

# A043/A047 공통 항목
ITM_BUNDLE_CARE = (
    "16350AAE6_15350AF1AA+16350AAE6_15350AF1AB+16350AAE6_15350AF1AC+"
    "16350AAE6_15350AF1AD+16350AAE6_15350AF1AE+16350AAE6_15350AF1AF+"
    "16350AAE6_15350AF1AG+16350AAE6_15350AF1AH+16350AAE6_15350AF1AI+"
)
ITM_A054 = "16350AAE4+16350AAA7+"  # 건수 + 진료비
ITM_N034 = "16118AAD8+"
ITM_A0611 = "16350AAB1+16350AAB3+16350AAA3+16350AAA4+16350AAA5+"
# 닫힌 298 질병 코드 묶음 (사용자 선택 목록 — 총계 포함)
OBJ_L1_A0611 = (
    "15350AF500+15350AF5AQ+15350AF5Ag+15350AF5BO+15350AF5BP+15350AF5BQ+"
    "15350AF5BR+15350AF5Bf+15350AF5Bm+15350AF5Bn+15350AF5Bs+15350AF5Bt+"
    "15350AF5Bz+15350AF5CB+15350AF5CC+15350AF5CE+15350AF5CH+15350AF5CK+"
    "15350AF5CL+15350AF5CM+15350AF5CN+15350AF5CO+15350AF5CT+15350AF5CU+"
    "15350AF5CW+15350AF5CY+15350AF5Ck+15350AF5Cp+15350AF5Cq+15350AF5Cw+"
    "15350AF5Cx+15350AF5Cy+15350AF5Cz+15350AF5DM+15350AF5DV+15350AF5EG+"
    "15350AF5EH+15350AF5Fa+15350AF5Fb+15350AF5Fc+"
)

# A054 고액 구간 (표 라벨 그대로)
HIGH_COST_BRACKETS = (
    "3000001~5000000원",
    "5000001~7000000원",
    "7000001~10000000원",
    "10000001원 이상",
)

SEX_TO_KOSIS = {"male": "남자", "female": "여자"}

# 닫힌 수술 매핑 (영역 → 건강보험 수술명). 의료 검수 draft.
AREA_SURGERY_LINKS = {
    "musculoskeletal_motion": [
        "슬관절 치환술",
        "일반 척추수술",
        "내시경하 척추수술",
        "하지정맥류 수술",
    ],
}

# 영역 → 298 질병분류명 (근사). HIRA KCD와 별개 보조 슬롯.
AREA_DISEASE_298 = {
    "head_neuro": ["편두통및기타두통증후군", "기타신경계의질환"],
    "musculoskeletal_motion": ["신경신경근및신경총장애"],
    "balance_metabolic": ["당뇨병", "비만", "기타비타민결핍증"],
    # 닫힌 298 목록 내 최근접(소화·배설 전용 항목 없음) — UI에 근사·한계 고지
    "gi_urine_stool": ["당뇨병", "비만"],
    "cardio_resp": ["급성심근경색증", "기타고혈압성질환", "폐렴"],
}

ITM_A0611_PERSONS = "진료실인원"
ITM_A0611_VISIT_DAYS = "내원일수"
ITM_A0611_BENEFIT_DAYS = "급여일수"
ITM_A0611_COST = "진료비"
ITM_A0611_BENEFIT = "급여비"

# A043 지표명
ITM_PATIENTS = "진료실인원수"
ITM_VISIT_DAYS = "입내원일수"
ITM_COST_PER_DAY = "입내원1일당 진료비"
# 수술 지표
ITM_SURG_PERSONS = "수술인원수"
ITM_SURG_CASES = "수술건수"
ITM_SURG_INPAT_DAYS = "입원일수"
ITM_SURG_COST = "진료비"


def _api_key() -> str:
    return (os.getenv("KOSIS_API_KEY") or "").strip()


def _cache_tbl_dir(tbl_id: str) -> Path:
    return CACHE_DIR / tbl_id


def _latest_path(tbl_id: str) -> Path:
    return _cache_tbl_dir(tbl_id) / "latest.json"


def load_table_cache(tbl_id: str) -> dict | None:
    path = _latest_path(tbl_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_table_cache(tbl_id: str, payload: dict) -> Path:
    d = _cache_tbl_dir(tbl_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "latest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "source": str(payload.get("source") or SOURCE),
        "tbl_id": tbl_id,
        "org_id": payload.get("org_id"),
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "latest_year": payload.get("latest_year"),
        "tbl_nm": payload.get("tbl_nm"),
        "cell_layout": payload.get("cell_layout"),
    }
    (CACHE_DIR / "meta.json").write_text(
        json.dumps(
            {
                **(json.loads((CACHE_DIR / "meta.json").read_text(encoding="utf-8"))
                   if (CACHE_DIR / "meta.json").is_file() else {}),
                tbl_id: meta,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _parse_number(raw: Any) -> float | None:
    if raw is None or raw == "" or raw == "-":
        return None
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_openapi(
        tbl_id: str,
        itm_id: str,
        *,
        org_id: str = ORG_NHIS,
        obj_l1: str = "ALL",
        obj_l2: str = "ALL",
        obj_l3: str = "",
        new_est: int = 3,
        prd_se: str = "Y",
        api_key: str | None = None) -> list[dict]:
    key = (api_key if api_key is not None else _api_key()).strip()
    if not key:
        raise RuntimeError("KOSIS_API_KEY가 없습니다")
    q = {
        "method": "getList",
        "apiKey": key,
        "format": "json",
        "jsonVD": "Y",
        "prdSe": prd_se,
        "newEstPrdCnt": str(new_est),
        "orgId": org_id,
        "tblId": tbl_id,
        "itmId": itm_id,
        "objL1": obj_l1,
        "objL2": obj_l2,
        "objL3": obj_l3,
        "objL4": "",
        "objL5": "",
        "objL6": "",
        "objL7": "",
        "objL8": "",
        "outputFields": (
            "ORG_ID+TBL_ID+TBL_NM+OBJ_ID+OBJ_NM+NM+"
            "ITM_ID+ITM_NM+UNIT_NM+PRD_DE+"
        ),
    }
    url = OPENAPI_URL + "?" + urllib.parse.urlencode(q, safe="+")
    req = urllib.request.Request(url, headers={"User-Agent": "harag-kosis/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, dict):
        raise RuntimeError(
            f"KOSIS error tbl={tbl_id}: {data.get('errMsg') or data}")
    if not isinstance(data, list):
        raise RuntimeError(f"KOSIS unexpected payload type: {type(data)}")
    return data


def _stamp(payload: dict) -> dict:
    payload["collected_at"] = datetime.now(timezone.utc).isoformat()
    payload["snapshot_id"] = payload["collected_at"][:10]
    payload["license"] = "KOSIS"
    return payload


def _persist(tbl_id: str, payload: dict) -> dict:
    _stamp(payload)
    save_table_cache(tbl_id, payload)
    try:
        from harag.guidance.stat_store import upsert_from_snapshot
        upsert_from_snapshot(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("stat_fact upsert %s failed: %s", tbl_id, e)
    return payload


def _normalize_age_sex(
        rows: list[dict], tbl_id: str, default_nm: str) -> dict:
    cells: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    tbl_nm = ""
    years: set[str] = set()
    for r in rows:
        tbl_nm = tbl_nm or str(r.get("TBL_NM") or "")
        year = str(r.get("PRD_DE") or "").strip()
        age = str(r.get("C1_NM") or "").strip()
        sex = str(r.get("C2_NM") or "").strip()
        itm = str(r.get("ITM_NM") or "").strip()
        val = _parse_number(r.get("DT"))
        if not year or not age or not sex or not itm or val is None:
            continue
        years.add(year)
        cells.setdefault(year, {}).setdefault(age, {}).setdefault(sex, {})[itm] = val
    latest = max(years) if years else ""
    return {
        "tbl_id": tbl_id,
        "tbl_nm": tbl_nm or default_nm,
        "source": SOURCE,
        "org_id": ORG_NHIS,
        "scheme": "health_insurance",
        "cell_layout": "age_sex_metric",
        "years": sorted(years),
        "latest_year": latest,
        "cells": cells,
    }


def _normalize_a043(rows: list[dict]) -> dict:
    return _normalize_age_sex(rows, TBL_A043, "연령별 성별 진료현황(총계)")


def _normalize_a047(rows: list[dict]) -> dict:
    return _normalize_age_sex(rows, TBL_A047, "연령별 성별 진료현황(입원)")


def _normalize_a054(rows: list[dict]) -> dict:
    """cells[year][age][bracket][metric] — C1=진료비구간, C2=연령."""
    cells: dict[str, Any] = {}
    tbl_nm = ""
    years: set[str] = set()
    for r in rows:
        tbl_nm = tbl_nm or str(r.get("TBL_NM") or "")
        year = str(r.get("PRD_DE") or "").strip()
        bracket = str(r.get("C1_NM") or "").strip()
        age = str(r.get("C2_NM") or "").strip()
        itm = str(r.get("ITM_NM") or "").strip()
        val = _parse_number(r.get("DT"))
        if not year or not bracket or not age or not itm or val is None:
            continue
        years.add(year)
        (cells.setdefault(year, {})
         .setdefault(age, {})
         .setdefault(bracket, {})[itm]) = val
    latest = max(years) if years else ""
    return {
        "tbl_id": TBL_A054,
        "tbl_nm": tbl_nm or "연령별 진료비 크기별 지급건수 및 진료비(입원)",
        "source": SOURCE,
        "org_id": ORG_NHIS,
        "scheme": "health_insurance",
        "cell_layout": "bracket_age",
        "years": sorted(years),
        "latest_year": latest,
        "cells": cells,
        "note": "KOSIS 수록 최신 연도가 오래된 표일 수 있음 — 연도를 반드시 표시.",
    }


def _normalize_n034(rows: list[dict]) -> dict:
    """cells[year][industry][size] = count. 규모 입력 없을 때는 size=전체만 조회."""
    cells: dict[str, Any] = {}
    tbl_nm = ""
    unit = "명"
    years: set[str] = set()
    for r in rows:
        tbl_nm = tbl_nm or str(r.get("TBL_NM") or "")
        unit = str(r.get("UNIT_NM") or unit) or unit
        year = str(r.get("PRD_DE") or "").strip()
        industry = str(r.get("C1_NM") or "").strip()
        size = str(r.get("C2_NM") or "").strip()
        val = _parse_number(r.get("DT"))
        if not year or not industry or not size or val is None:
            continue
        years.add(year)
        cells.setdefault(year, {}).setdefault(industry, {})[size] = val
    latest = max(years) if years else ""
    return {
        "tbl_id": TBL_N034,
        "tbl_nm": tbl_nm or "업무상질병 발생현황-규모별",
        "source": SOURCE_MOL,
        "org_id": ORG_MOL,
        "unit": unit,
        "cell_layout": "industry_size",
        "years": sorted(years),
        "latest_year": latest,
        "cells": cells,
        "note": (
            "업종×사업장 규모 표. 앱은 규모=전체만 사용. "
            "질병 유형·직종 세분은 없음. 산재 승인 건수 ≠ 발생률."
        ),
    }


def _normalize_a0611(rows: list[dict]) -> dict:
    """cells[year][disease][inout][age][metric]. C1=질병 C2=입원외래 C3=연령."""
    cells: dict[str, Any] = {}
    tbl_nm = ""
    years: set[str] = set()
    for r in rows:
        tbl_nm = tbl_nm or str(r.get("TBL_NM") or "")
        year = str(r.get("PRD_DE") or "").strip()
        disease = str(r.get("C1_NM") or "").strip()
        inout = str(r.get("C2_NM") or "").strip()
        age = str(r.get("C3_NM") or "").strip()
        itm = str(r.get("ITM_NM") or "").strip()
        val = _parse_number(r.get("DT"))
        if not year or not disease or not inout or not age or not itm or val is None:
            continue
        years.add(year)
        (cells.setdefault(year, {})
         .setdefault(disease, {})
         .setdefault(inout, {})
         .setdefault(age, {})[itm]) = val
    latest = max(years) if years else ""
    return {
        "tbl_id": TBL_A0611,
        "tbl_nm": tbl_nm or "298 질병분류별 연령별 급여현황(총계 - 5세단위)",
        "source": SOURCE,
        "org_id": ORG_NHIS,
        "scheme": "health_insurance",
        "cell_layout": "disease_inout_age",
        "years": sorted(years),
        "latest_year": latest,
        "cells": cells,
        "note": (
            "298 질병분류·입원외래·연령. OpenAPI 수록 연도가 오래될 수 있음. "
            "질병군 간 진료실인원 중복 가능 — 단순 합산 금지."
        ),
    }


def _normalize_surgery(rows: list[dict], tbl_id: str, scheme: str) -> dict:
    """age×sex×surgery×metric. age가 계만 있어도 허용."""
    cells: dict[str, Any] = {}
    tbl_nm = ""
    years: set[str] = set()
    for r in rows:
        tbl_nm = tbl_nm or str(r.get("TBL_NM") or "")
        year = str(r.get("PRD_DE") or "").strip()
        age = str(r.get("C1_NM") or "").strip() or "계"
        sex = str(r.get("C2_NM") or "").strip()
        surg = str(r.get("C3_NM") or "").strip()
        itm = str(r.get("ITM_NM") or "").strip()
        val = _parse_number(r.get("DT"))
        if not year or not sex or not surg or not itm or val is None:
            continue
        years.add(year)
        (cells.setdefault(year, {})
         .setdefault(age, {})
         .setdefault(sex, {})
         .setdefault(surg, {})[itm]) = val
    latest = max(years) if years else ""
    return {
        "tbl_id": tbl_id,
        "tbl_nm": tbl_nm,
        "source": SOURCE,
        "org_id": ORG_NHIS,
        "scheme": scheme,
        "cell_layout": "surgery",
        "years": sorted(years),
        "latest_year": latest,
        "cells": cells,
    }


def sync_a043(api_key: str | None = None) -> dict:
    rows = fetch_openapi(
        TBL_A043, ITM_BUNDLE_CARE, org_id=ORG_NHIS,
        obj_l1="ALL", obj_l2="ALL", obj_l3="",
        new_est=3, api_key=api_key)
    return _persist(TBL_A043, _normalize_a043(rows))


def sync_a047(api_key: str | None = None) -> dict:
    rows = fetch_openapi(
        TBL_A047, ITM_BUNDLE_CARE, org_id=ORG_NHIS,
        obj_l1="ALL", obj_l2="ALL", obj_l3="",
        new_est=3, api_key=api_key)
    return _persist(TBL_A047, _normalize_a047(rows))


def sync_a054(api_key: str | None = None) -> dict:
    rows = fetch_openapi(
        TBL_A054, ITM_A054, org_id=ORG_NHIS,
        obj_l1="ALL", obj_l2="ALL", obj_l3="",
        new_est=10, api_key=api_key)
    return _persist(TBL_A054, _normalize_a054(rows))


def sync_n034(api_key: str | None = None) -> dict:
    rows = fetch_openapi(
        TBL_N034, ITM_N034, org_id=ORG_MOL,
        obj_l1="ALL", obj_l2="ALL", obj_l3="",
        new_est=3, api_key=api_key)
    return _persist(TBL_N034, _normalize_n034(rows))


def sync_a0611(api_key: str | None = None) -> dict:
    """질병 목록은 닫힌 코드, 연령·입원외래는 ALL (URL의 0~9세만 쓰면 성인 불가)."""
    rows = fetch_openapi(
        TBL_A0611, ITM_A0611, org_id=ORG_NHIS,
        obj_l1=OBJ_L1_A0611, obj_l2="ALL", obj_l3="ALL",
        new_est=5, api_key=api_key)
    return _persist(TBL_A0611, _normalize_a0611(rows))


def sync_surgery_table(
        tbl_id: str,
        scheme: str,
        api_key: str | None = None) -> dict:
    """연령=계, 성별×수술종류 전체 (셀 한도 내)."""
    itm = (
        "16350AAA0+16350AAA1+16350AAA2+16350AAA3+16350AAA4+16350AAA5+"
    )
    rows = fetch_openapi(
        tbl_id, itm, org_id=ORG_NHIS,
        obj_l1="11101YRE000+",  # 연령 계
        obj_l2="ALL",
        obj_l3="ALL",
        new_est=3,
        api_key=api_key,
    )
    return _persist(tbl_id, _normalize_surgery(rows, tbl_id, scheme))


def sync_all(api_key: str | None = None) -> dict[str, str]:
    """캐시·stat_fact 갱신. 실패 표는 건너뛰고 이전 캐시 유지."""
    out: dict[str, str] = {}
    jobs = [
        (TBL_A043, lambda: sync_a043(api_key=api_key)),
        (TBL_A047, lambda: sync_a047(api_key=api_key)),
        (TBL_A054, lambda: sync_a054(api_key=api_key)),
        (TBL_A0611, lambda: sync_a0611(api_key=api_key)),
        (TBL_N034, lambda: sync_n034(api_key=api_key)),
        (TBL_A001, lambda: sync_surgery_table(
            TBL_A001, "health_insurance", api_key=api_key)),
        (TBL_A002, lambda: sync_surgery_table(
            TBL_A002, "medical_aid", api_key=api_key)),
    ]
    for tbl, fn in jobs:
        try:
            fn()
            out[tbl] = "ok"
        except Exception as e:  # noqa: BLE001
            logger.warning("kosis sync %s failed: %s", tbl, e)
            out[tbl] = f"fail:{type(e).__name__}"
    return out


def _sum_metrics(
        cells: dict,
        year: str,
        ages: list[str],
        sex_label: str,
        metrics: list[str]) -> dict[str, float] | None:
    """여러 연령 구간 합산. 일당 진료비는 가중 평균(입내원일수 가중)."""
    acc = {m: 0.0 for m in metrics}
    day_w = 0.0
    cost_day_acc = 0.0
    found = False
    year_cells = cells.get(year) or {}
    for age in ages:
        sex_map = (year_cells.get(age) or {}).get(sex_label) or {}
        if not sex_map:
            continue
        found = True
        for m in metrics:
            if m == ITM_COST_PER_DAY:
                continue
            v = sex_map.get(m)
            if v is not None:
                acc[m] = acc.get(m, 0.0) + float(v)
        days = sex_map.get(ITM_VISIT_DAYS)
        cpd = sex_map.get(ITM_COST_PER_DAY)
        if days is not None and cpd is not None:
            day_w += float(days)
            cost_day_acc += float(days) * float(cpd)
    if not found:
        return None
    if day_w > 0 and ITM_COST_PER_DAY in metrics:
        acc[ITM_COST_PER_DAY] = cost_day_acc / day_w
    return acc


def care_context_for(
        age_band: str | None,
        sex: str | None) -> dict[str, Any]:
    """A043 → 이용·비용 맥락 라벨. 캐시만."""
    missing = {
        "missing": True,
        "label": "찾지 못했어요",
        "detail": "",
        "source": SOURCE,
        "year": None,
        "scheme": "health_insurance",
    }
    cache = load_table_cache(TBL_A043)
    if not cache:
        return missing
    ages = kosis_ages(age_band, "default")
    sex_label = SEX_TO_KOSIS.get(sex or "")
    if not ages or not sex_label:
        return {
            **missing,
            "detail": "연령·성별을 선택하면 진료 이용·비용 참고를 붙입니다.",
        }
    year = str(cache.get("latest_year") or "")
    cells = cache.get("cells") or {}
    summed = _sum_metrics(
        cells, year, ages, sex_label,
        [ITM_PATIENTS, ITM_VISIT_DAYS, ITM_COST_PER_DAY])
    if not summed:
        return missing
    patients = summed.get(ITM_PATIENTS) or 0.0
    days = summed.get(ITM_VISIT_DAYS) or 0.0
    cpd = summed.get(ITM_COST_PER_DAY)
    per_person_days = (days / patients) if patients > 0 else None
    # 비용 밴드 (원 단위; API는 원)
    if cpd is None:
        cost_label = "찾지 못했어요"
    elif cpd < 50_000:
        cost_label = "입내원 1일당 진료비가 낮은 편"
    elif cpd < 100_000:
        cost_label = "입내원 1일당 진료비가 중간 정도"
    else:
        cost_label = "입내원 1일당 진료비가 높은 편"
    intensity = ""
    if per_person_days is not None:
        if per_person_days < 8:
            intensity = "1인당 입내원일이 짧은 편"
        elif per_person_days < 15:
            intensity = "1인당 입내원일이 보통"
        else:
            intensity = "1인당 입내원일이 긴 편"
    age_ui = age_ui_label(age_band)
    sex_ui = {"male": "남성", "female": "여성"}.get(sex or "", "")
    who = " · ".join(x for x in (age_ui, sex_ui) if x)
    parts = [p for p in (intensity, cost_label) if p and p != "찾지 못했어요"]
    label = " · ".join(parts) if parts else "찾지 못했어요"
    detail_bits = []
    if per_person_days is not None:
        detail_bits.append(f"1인당 입내원 약 {per_person_days:.1f}일")
    if cpd is not None:
        detail_bits.append(f"입내원 1일당 진료비 약 {int(round(cpd)):,}원")
    inpatient = care_inpatient_context_for(age_band, sex)
    out = {
        "missing": label == "찾지 못했어요",
        "label": label,
        "detail": f"{who} 전체 진료 이용 참고 — " + ", ".join(detail_bits),
        "source": f"{SOURCE}, {year}",
        "year": year,
        "scheme": "health_insurance",
        "patients": patients,
        "visit_days": days,
        "cost_per_visit_day": cpd,
        "per_person_visit_days": per_person_days,
        "tbl_id": TBL_A043,
        "note": "상병(KCD) 유병·확진이 아니라 해당 연령·성별 전체 진료 이용 통계입니다.",
        "inpatient": inpatient,
    }
    if not inpatient.get("missing") and inpatient.get("label"):
        out["label"] = (
            f"{label} · 입원: {inpatient['label']}"
            if label != "찾지 못했어요"
            else f"입원: {inpatient['label']}"
        )
        out["missing"] = False
    return out


def care_inpatient_context_for(
        age_band: str | None,
        sex: str | None) -> dict[str, Any]:
    """A047 입원 진료현황 — 총계(A043)와 구분."""
    missing = {
        "missing": True,
        "label": "찾지 못했어요",
        "detail": "",
        "source": SOURCE,
        "year": None,
        "scheme": "health_insurance",
        "tbl_id": TBL_A047,
    }
    cache = load_table_cache(TBL_A047)
    if not cache:
        return missing
    ages = kosis_ages(age_band, "default")
    sex_label = SEX_TO_KOSIS.get(sex or "")
    if not ages or not sex_label:
        return {
            **missing,
            "detail": "연령·성별을 선택하면 입원 이용 참고를 붙입니다.",
        }
    year = str(cache.get("latest_year") or "")
    cells = cache.get("cells") or {}
    summed = _sum_metrics(
        cells, year, ages, sex_label,
        [ITM_PATIENTS, ITM_VISIT_DAYS, ITM_COST_PER_DAY])
    if not summed:
        return missing
    patients = summed.get(ITM_PATIENTS) or 0.0
    days = summed.get(ITM_VISIT_DAYS) or 0.0
    cpd = summed.get(ITM_COST_PER_DAY)
    per_person_days = (days / patients) if patients > 0 else None
    if per_person_days is None:
        intensity = "찾지 못했어요"
    elif per_person_days < 8:
        intensity = "1인당 입원일이 짧은 편"
    elif per_person_days < 15:
        intensity = "1인당 입원일이 보통"
    else:
        intensity = "1인당 입원일이 긴 편"
    if cpd is None:
        cost_label = ""
    elif cpd < 100_000:
        cost_label = "입원 1일당 진료비가 낮은 편"
    elif cpd < 200_000:
        cost_label = "입원 1일당 진료비가 중간 정도"
    else:
        cost_label = "입원 1일당 진료비가 높은 편"
    parts = [p for p in (intensity, cost_label) if p and p != "찾지 못했어요"]
    label = " · ".join(parts) if parts else "찾지 못했어요"
    detail_bits = []
    if per_person_days is not None:
        detail_bits.append(f"1인당 입원 약 {per_person_days:.1f}일")
    if cpd is not None:
        detail_bits.append(f"입원 1일당 진료비 약 {int(round(cpd)):,}원")
    return {
        "missing": label == "찾지 못했어요",
        "label": label,
        "detail": "입원 진료 이용 참고 — " + ", ".join(detail_bits),
        "source": f"{SOURCE}, {year}",
        "year": year,
        "scheme": "health_insurance",
        "patients": patients,
        "visit_days": days,
        "cost_per_visit_day": cpd,
        "per_person_visit_days": per_person_days,
        "tbl_id": TBL_A047,
        "note": "입원 청구 집단 통계이며 특정 상병 유병이 아닙니다.",
    }


def cost_bracket_context_for(age_band: str | None) -> dict[str, Any]:
    """A054 연령×진료비 구간(입원) — 고액 구간 건수 비중."""
    missing = {
        "missing": True,
        "label": "찾지 못했어요",
        "detail": "",
        "source": SOURCE,
        "year": None,
        "scheme": "health_insurance",
        "tbl_id": TBL_A054,
    }
    cache = load_table_cache(TBL_A054)
    if not cache:
        return missing
    ages = kosis_ages(age_band, "a054")
    if not ages:
        return {
            **missing,
            "detail": "연령을 선택하면 입원 진료비 구간 참고를 붙입니다.",
        }
    year = str(cache.get("latest_year") or "")
    year_cells = (cache.get("cells") or {}).get(year) or {}
    total_cases = 0.0
    high_cases = 0.0
    found = False
    for age in ages:
        brackets = year_cells.get(age) or {}
        if not brackets:
            continue
        found = True
        for bracket, metrics in brackets.items():
            if not isinstance(metrics, dict):
                continue
            if bracket in ("계", "합계"):
                continue
            n = metrics.get("건수")
            if n is None:
                continue
            n = float(n)
            total_cases += n
            if bracket in HIGH_COST_BRACKETS:
                high_cases += n
    if not found or total_cases <= 0:
        return missing
    share = 100.0 * high_cases / total_cases
    if share < 5:
        band = "고액 입원 구간 비중이 낮은 편"
    elif share < 15:
        band = "고액 입원 구간 비중이 보통"
    else:
        band = "고액 입원 구간 비중이 높은 편"
    age_ui = age_ui_label(age_band)
    return {
        "missing": False,
        "label": band,
        "detail": (
            f"{age_ui} 입원 지급건수 중 고액 구간(300만원 초과) 약 {share:.1f}%"
        ),
        "source": f"{SOURCE}, {year}",
        "year": year,
        "scheme": "health_insurance",
        "high_share_pct": share,
        "tbl_id": TBL_A054,
        "note": (
            "입원 진료비 구간 통계이며 보장 한도·상품 추천이 아닙니다. "
            f"표 수록 연도({year})를 확인하세요."
        ),
    }


def _surgery_row(
        cache: dict,
        year: str,
        sex_label: str,
        surgery: str) -> dict[str, float] | None:
    cells = cache.get("cells") or {}
    # prefer age 계
    age_map = (cells.get(year) or {}).get("계") or {}
    sex_map = age_map.get(sex_label) or age_map.get("계") or {}
    row = sex_map.get(surgery)
    return row if isinstance(row, dict) else None


def surgery_context_for(
        area_ids: list[str],
        sex: str | None,
        *,
        scheme: str = "health_insurance") -> dict[str, Any]:
    """닫힌 수술 매핑 → 평균 입원일·비용 밴드. A001 기본, A002는 medical_aid."""
    tbl = TBL_A001 if scheme == "health_insurance" else TBL_A002
    missing = {
        "missing": True,
        "label": "찾지 못했어요",
        "detail": "",
        "source": SOURCE,
        "year": None,
        "scheme": scheme,
        "surgeries": [],
    }
    cache = load_table_cache(tbl)
    if not cache:
        return missing
    sex_label = SEX_TO_KOSIS.get(sex or "") or "계"
    year = str(cache.get("latest_year") or "")
    names: list[str] = []
    for aid in area_ids:
        for n in AREA_SURGERY_LINKS.get(aid) or []:
            if n not in names:
                names.append(n)
    if not names:
        return {**missing, "detail": "연결된 참고 수술이 없습니다."}

    picked: list[dict] = []
    for name in names:
        row = _surgery_row(cache, year, sex_label, name)
        if not row:
            row = _surgery_row(cache, year, "계", name)
        if not row:
            continue
        persons = row.get(ITM_SURG_PERSONS)
        days = row.get(ITM_SURG_INPAT_DAYS)
        cost = row.get(ITM_SURG_COST)  # 천원
        avg_days = None
        if persons and days and persons > 0:
            avg_days = float(days) / float(persons)
        cost_per = None
        if persons and cost and persons > 0:
            cost_per = float(cost) * 1000.0 / float(persons)
        picked.append({
            "name": name,
            "avg_inpatient_days": avg_days,
            "cost_per_person_krw": cost_per,
            "persons": persons,
        })
    if not picked:
        return missing

    # 대표: 평균 입원일 최대
    picked.sort(
        key=lambda x: (x.get("avg_inpatient_days") is not None,
                       x.get("avg_inpatient_days") or 0),
        reverse=True)
    top = picked[0]
    avg_d = top.get("avg_inpatient_days")
    if avg_d is None:
        stay_label = "찾지 못했어요"
    elif avg_d <= 3:
        stay_label = "평균 입원이 짧은 편"
    elif avg_d <= 7:
        stay_label = "평균 입원이 보통"
    else:
        stay_label = "평균 입원이 긴 편"
    scheme_ko = (
        "건강보험" if scheme == "health_insurance" else "의료급여"
    )
    return {
        "missing": False,
        "label": f"{top['name']} 기준 {stay_label}",
        "detail": (
            f"{scheme_ko} 수술 통계 — "
            + (
                f"1인당 평균 입원 약 {avg_d:.1f}일"
                if avg_d is not None else "입원일 없음"
            )
        ),
        "source": f"{SOURCE}, {year}",
        "year": year,
        "scheme": scheme,
        "scheme_label": scheme_ko,
        "surgeries": picked[:4],
        "tbl_id": tbl,
        "note": (
            "특정 증상의 수술 필요를 뜻하지 않습니다. "
            f"{scheme_ko} 청구 집단의 수술 현황 참고입니다."
        ),
    }


# 영역별 298 근사 한계 고지 (전용 항목이 없을 때)
_AREA_DISEASE_298_SOFT_NOTE = {
    "gi_urine_stool": (
        "소화·배설 전용 298 항목이 없어 대사 질환(당뇨·비만)으로 근사 연결합니다."
    ),
    "cardio_resp": (
        "호흡·순환 증상을 심근경색·고혈압·폐렴 급여 통계에 근사 연결합니다."
    ),
}


def disease_298_context_for(
        area_id: str | None,
        age_band: str | None) -> dict[str, Any]:
    """A0611 — 영역 연결 질병의 연령대 입원 비중(진료실인원)."""
    soft = _AREA_DISEASE_298_SOFT_NOTE.get(area_id or "") or ""
    missing = {
        "missing": True,
        "label": "찾지 못했어요",
        "detail": "",
        "source": SOURCE,
        "year": None,
        "scheme": "health_insurance",
        "tbl_id": TBL_A0611,
        "disease": None,
        "match": "approx",
        "reason": "",
    }
    diseases = AREA_DISEASE_298.get(area_id or "") or []
    if not diseases:
        return {
            **missing,
            "reason": "이 영역에 연결한 298 질병분류가 없습니다.",
            "detail": "이 영역에 연결한 298 질병분류가 없습니다.",
        }
    cache = load_table_cache(TBL_A0611)
    if not cache:
        return {
            **missing,
            "reason": "질병분류(A0611) 캐시가 없습니다.",
            "detail": "질병분류(A0611) 캐시가 없습니다.",
        }
    ages = kosis_ages(age_band, "a0611")
    if not ages:
        return {
            **missing,
            "detail": "연령을 선택하면 질병분류 급여 참고를 붙입니다.",
            "reason": "연령을 선택하면 질병분류 급여 참고를 붙입니다.",
        }
    year = str(cache.get("latest_year") or "")
    year_cells = (cache.get("cells") or {}).get(year) or {}

    picked_name = None
    in_n = 0.0
    out_n = 0.0
    for name in diseases:
        dcells = year_cells.get(name) or {}
        if not dcells:
            continue
        inn = 0.0
        outt = 0.0
        for age in ages:
            inn += float(
                ((dcells.get("입원") or {}).get(age) or {})
                .get(ITM_A0611_PERSONS) or 0)
            outt += float(
                ((dcells.get("외래") or {}).get(age) or {})
                .get(ITM_A0611_PERSONS) or 0)
        if inn + outt <= 0:
            continue
        picked_name = name
        in_n, out_n = inn, outt
        break
    if not picked_name or (in_n + out_n) <= 0:
        return {
            **missing,
            "reason": "매핑한 298 질병의 해당 연령 구간 값이 없습니다.",
            "detail": "매핑한 298 질병의 해당 연령 구간 값이 없습니다.",
        }

    share = 100.0 * in_n / (in_n + out_n)
    if share < 10:
        band = "해당 연령대에서 외래 비중이 큰 편"
    elif share < 40:
        band = "해당 연령대에서 입원·외래가 섞인 편"
    else:
        band = "해당 연령대에서 입원 비중이 큰 편"
    age_ui = age_ui_label(age_band)
    note = (
        "298 질병분류 급여 통계이며 KCD 확진·유병이 아닙니다. "
        f"수록 연도({year})·근사 연결을 확인하세요."
    )
    if soft:
        note = f"{note} {soft}"
    return {
        "missing": False,
        "label": f"{picked_name} — {band}",
        "detail": (
            f"{age_ui} 진료실인원 기준 입원 비중 약 {share:.1f}% "
            f"(입원 {int(round(in_n)):,} · 외래 {int(round(out_n)):,})"
        ),
        "source": f"{SOURCE}, {year}",
        "year": year,
        "scheme": "health_insurance",
        "tbl_id": TBL_A0611,
        "disease": picked_name,
        "inpatient_share_pct": share,
        "match": "approx",
        "note": note,
        "reason": "",
    }


def build_kosis_bundle(
        area_ids: list[str],
        age_band: str | None,
        sex: str | None) -> dict[str, Any]:
    """결과 API용 묶음 — guide()에서 HTTP 없음."""
    care = care_context_for(age_band, sex)
    nhi_surg = surgery_context_for(
        area_ids, sex, scheme="health_insurance")
    aid_surg = surgery_context_for(
        area_ids, sex, scheme="medical_aid")
    cost = cost_bracket_context_for(age_band)
    by_area = {
        aid: disease_298_context_for(aid, age_band)
        for aid in area_ids if aid
    }
    return {
        "care_context": care,
        "surgery_nhi": nhi_surg,
        "surgery_medical_aid": aid_surg,
        "cost_bracket": cost,
        "disease_298_by_area": by_area,
        "source": SOURCE,
    }


def ensure_stat_db_from_caches() -> dict[str, int]:
    """기존 JSON 캐시를 SQLite에 적재 (기동·테스트용)."""
    from harag.guidance.stat_store import load_all_caches_into_db
    return load_all_caches_into_db()


def main_sync() -> None:
    """python -m harag.guidance.kosis_nhis"""
    status = sync_all()
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main_sync()
