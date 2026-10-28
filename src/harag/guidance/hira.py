"""심평원 질병정보 — 닫힌 KCD만 조회. 사용자 문장은 보내지 않는다."""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from harag.guidance.engine import RuleResult
from harag.guidance.lexicon import load_kb, ref_conditions_of

_AGE = {
    "0-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70+",
}
_SEX = {"male", "female"}

logger = logging.getLogger("harag.hira")

SOURCE = "건강보험심사평가원 질병정보서비스"
CACHE_DIR = Path(__file__).with_name("hira_cache")
DEFAULT_BASE = "https://apis.data.go.kr/B551182/diseaseInfoService1"
TIMEOUT_S = 2.5
STAT_YEAR = 2023
MAX_STATS = 3


def normalize_kcd(kcd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", kcd or "").upper()


def _cache_path(kcd: str) -> Path:
    return CACHE_DIR / f"{normalize_kcd(kcd)}.json"


def load_cache(kcd: str) -> dict | None:
    path = _cache_path(kcd)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("kcd"):
        return None
    return data


def _local_name(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _xml_items(text: str) -> list[dict]:
    root = ET.fromstring(text)
    rows: list[dict] = []
    for item in root.findall(".//item"):
        row = {}
        for child in list(item):
            row[_local_name(child.tag)] = (child.text or "").strip()
        if row:
            rows.append(row)
    return rows


def _first_int(row: dict, *keys: str) -> int | None:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        digits = re.sub(r"[^\d]", "", str(raw))
        if digits:
            return int(digits)
    return None


def _row_int(row: dict) -> int | None:
    return _first_int(
        row, "ptntcnt", "cnt", "totcnt", "rtrvcnt", "ncs", "mdcnt", "qty")


def _coverage_link(inpatient_share: float | None, labels: list[str]) -> str:
    if inpatient_share is None:
        return ""
    if inpatient_share < 0.5 and "실손 통원" in labels:
        return "통원 청구가 많아 실손 통원과 함께 봅니다."
    if inpatient_share >= 0.5 and "실손 입원" in labels:
        return "입원 청구 비중이 있어 실손 입원과 함께 봅니다."
    return ""


class HiraStats:
    def __init__(self, service_key: str = "", base_url: str = DEFAULT_BASE,
                 timeout_s: float = TIMEOUT_S):
        self._key = (service_key or "").strip()
        self._base = (base_url or DEFAULT_BASE).rstrip("/")
        self._timeout = timeout_s
        self._live_ok = True

    @classmethod
    def from_settings(cls, settings) -> "HiraStats":
        return cls(
            service_key=getattr(settings, "hira_service_key", "") or "",
            base_url=getattr(settings, "hira_base_url", "") or DEFAULT_BASE,
        )

    def lookup(self, rules: RuleResult, age_band: str | None = None,
               sex: str | None = None) -> list[dict]:
        kb = load_kb()
        specs = kb.get("symptoms") or {}
        seen: set[str] = set()
        conds: list[dict] = []
        for hit in rules.symptoms:
            for cond in ref_conditions_of(specs.get(hit.code)):
                key = normalize_kcd(cond["kcd"])
                if not key or key in seen:
                    continue
                seen.add(key)
                conds.append(cond)
                if len(conds) >= MAX_STATS:
                    break
            if len(conds) >= MAX_STATS:
                break
        labels = [c.label for c in rules.coverages]
        out: list[dict] = []
        for cond in conds:
            row = self._one(cond, age_band=age_band, sex=sex, labels=labels)
            if row:
                out.append(row)
        return out

    def _one(self, cond: dict, age_band: str | None, sex: str | None,
             labels: list[str]) -> dict | None:
        cached = load_cache(cond["kcd"])
        live = None
        if self._key and self._live_ok and cached is None:
            live = self._try_live(cond["kcd"])
        data = live or cached
        if not data:
            return None
        share = data.get("inpatient_share")
        try:
            share_f = float(share) if share is not None else None
        except (TypeError, ValueError):
            share_f = None
        age_count = None
        if age_band in _AGE and sex in _SEX:
            by_age = data.get("by_age_sex") or {}
            raw = by_age.get(f"{age_band}|{sex}")
            if raw is not None:
                try:
                    age_count = int(raw)
                except (TypeError, ValueError):
                    age_count = None
        total = data.get("total_count")
        try:
            total_i = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_i = None
        return {
            "kcd": cond["kcd"],
            "name": data.get("name") or cond["name"],
            "inpatient_share": share_f,
            "age_sex_count": age_count,
            "total_count": total_i,
            "coverage_link": _coverage_link(share_f, labels),
            "source": data.get("source") or SOURCE,
            "cached": live is None,
        }

    def _try_live(self, kcd: str) -> dict | None:
        sick = normalize_kcd(kcd)
        if not sick or not re.fullmatch(r"[A-Z][0-9]{2,4}", sick):
            return None
        try:
            gender_items = self._request(
                "getDissByGenderAgeStats1",
                {"sickCd": sick, "year": STAT_YEAR, "sickType": 1,
                 "medTp": 1, "pageNo": 1, "numOfRows": 100},
            )
            inout_items = self._request(
                "getDissByHsptlzFrgnStats1",
                {"sickCd": sick, "year": STAT_YEAR, "sickType": 1,
                 "medTp": 1, "pageNo": 1, "numOfRows": 20},
            )
        except Exception as e:  # noqa: BLE001
            logger.info("hira live skip: %s", type(e).__name__)
            self._live_ok = False
            return None
        totals = [n for n in (_row_int(r) for r in gender_items) if n is not None]
        if not totals:
            return None
        inp = _inout_share(inout_items)
        return {
            "kcd": kcd,
            "name": "",
            "total_count": sum(totals),
            "inpatient_share": inp,
            "by_age_sex": {},
            "source": SOURCE,
        }

    def _request(self, operation: str, params: dict) -> list[dict]:
        url = f"{self._base}/{operation}"
        query = {
            "serviceKey": self._key,
            "ServiceKey": self._key,
            "_type": "xml",
            **{k: str(v) for k, v in params.items()},
        }
        with httpx.Client(timeout=self._timeout) as client:
            res = client.get(url, params=query)
        res.raise_for_status()
        return _xml_items(res.text)


def _inout_share(items: list[dict]) -> float | None:
    inp = 0
    outp = 0
    for row in items:
        blob = " ".join(f"{k} {v}" for k, v in row.items()).lower()
        n = _row_int(row) or 0
        if "입원" in blob or "hsptlz" in blob or "inp" in blob:
            inp += n
        if "외래" in blob or "통원" in blob or "otpt" in blob or "outp" in blob:
            outp += n
    denom = inp + outp
    if denom <= 0:
        return None
    return round(inp / denom, 3)
