"""통계 수치 SQLite 저장소 — JSON 스냅샷을 적재하고 get_stat으로 키 조회.

문서 MetadataStore와 분리. guide()는 OpenAPI 없이 여기/캐시만 읽는다.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Float,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger("harag.stat_store")

DEFAULT_DSN = "sqlite:///./data/guidance_stats.db"


class Base(DeclarativeBase):
    pass


class StatFact(Base):
    __tablename__ = "stat_fact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128), default="")
    table_id: Mapped[str] = mapped_column(String(64), index=True)
    table_name: Mapped[str] = mapped_column(String(256), default="")
    period: Mapped[str] = mapped_column(String(32), index=True)
    dims: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    metric: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(64), default="")
    collected_at: Mapped[str] = mapped_column(String(64), default="")
    snapshot_id: Mapped[str] = mapped_column(String(64), default="")
    license: Mapped[str] = mapped_column(String(128), default="")


@dataclass(frozen=True)
class StatResult:
    value: float
    unit: str
    period: str
    source: str
    table_id: str
    table_name: str
    collected_at: str
    dims: dict[str, str]
    metric: str


_engine = None
_Session: sessionmaker | None = None
_mem: dict[str, StatResult | None] = {}


def _dsn() -> str:
    return (os.getenv("GUIDANCE_STATS_DSN") or DEFAULT_DSN).strip() or DEFAULT_DSN


def _ensure_engine():
    global _engine, _Session
    if _engine is not None:
        return
    dsn = _dsn()
    if dsn.startswith("sqlite:///./") or dsn.startswith("sqlite:///"):
        raw = dsn.removeprefix("sqlite:///")
        if raw and raw != ":memory:":
            Path(raw).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if dsn.startswith("sqlite") else {}
    _engine = create_engine(dsn, future=True, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def _dims_key(dims: dict[str, str] | None) -> str:
    d = {str(k): str(v) for k, v in (dims or {}).items() if v is not None}
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _cache_key(table_id: str, metric: str, dims: dict | None, period: str) -> str:
    return f"{table_id}|{metric}|{_dims_key(dims)}|{period}"


def clear_memory_cache() -> None:
    _mem.clear()


def upsert_rows(table_id: str, rows: list[dict[str, Any]]) -> int:
    """Replace all facts for table_id with rows."""
    _ensure_engine()
    assert _Session is not None
    clear_memory_cache()
    with _Session() as session:
        session.execute(delete(StatFact).where(StatFact.table_id == table_id))
        for r in rows:
            session.add(
                StatFact(
                    source=str(r.get("source") or ""),
                    table_id=table_id,
                    table_name=str(r.get("table_name") or ""),
                    period=str(r.get("period") or ""),
                    dims=_dims_key(r.get("dims") or {}),
                    metric=str(r.get("metric") or ""),
                    value=float(r["value"]),
                    unit=str(r.get("unit") or ""),
                    collected_at=str(r.get("collected_at") or ""),
                    snapshot_id=str(r.get("snapshot_id") or ""),
                    license=str(r.get("license") or ""),
                )
            )
        session.commit()
    return len(rows)


def upsert_from_snapshot(payload: dict[str, Any]) -> int:
    """Flatten kosis_cache-style snapshot into stat_fact rows."""
    tbl_id = str(payload.get("tbl_id") or "")
    if not tbl_id:
        return 0
    source = str(payload.get("source") or "")
    tbl_nm = str(payload.get("tbl_nm") or "")
    unit_default = str(payload.get("unit") or "")
    collected = str(
        payload.get("collected_at")
        or datetime.now(timezone.utc).isoformat()
    )
    snap = str(payload.get("snapshot_id") or collected[:10])
    license_ = str(payload.get("license") or "KOSIS")
    cells = payload.get("cells") or {}
    kind = str(payload.get("cell_layout") or "")
    if not kind:
        if tbl_id == "DT_11806_N034":
            kind = "industry_size"
        elif tbl_id in ("TX_35001_A043", "TX_35001_A047"):
            kind = "age_sex_metric"
        elif tbl_id == "TX_35001_A054":
            kind = "bracket_age"
        elif tbl_id in ("TX_35004_A001", "TX_35004_A002"):
            kind = "surgery"
        elif tbl_id == "DT_35001_A0611":
            kind = "disease_inout_age"
    rows: list[dict[str, Any]] = []

    if kind == "industry_size":
        for year, industries in cells.items():
            if not isinstance(industries, dict):
                continue
            for industry, sizes in industries.items():
                if not isinstance(sizes, dict):
                    continue
                for size, val in sizes.items():
                    if not isinstance(val, (int, float)):
                        continue
                    rows.append({
                        "source": source,
                        "table_name": tbl_nm,
                        "period": str(year),
                        "dims": {"industry": str(industry), "size": str(size)},
                        "metric": "count",
                        "value": float(val),
                        "unit": unit_default or "명",
                        "collected_at": collected,
                        "snapshot_id": snap,
                        "license": license_,
                    })
    elif kind == "age_sex_metric":
        for year, ages in cells.items():
            if not isinstance(ages, dict):
                continue
            for age, sexes in ages.items():
                if not isinstance(sexes, dict):
                    continue
                for sex, metrics in sexes.items():
                    if not isinstance(metrics, dict):
                        continue
                    for metric, val in metrics.items():
                        if not isinstance(val, (int, float)):
                            continue
                        rows.append({
                            "source": source,
                            "table_name": tbl_nm,
                            "period": str(year),
                            "dims": {"age": str(age), "sex": str(sex)},
                            "metric": str(metric),
                            "value": float(val),
                            "unit": unit_default,
                            "collected_at": collected,
                            "snapshot_id": snap,
                            "license": license_,
                        })
    elif kind == "bracket_age":
        for year, ages in cells.items():
            if not isinstance(ages, dict):
                continue
            for age, brackets in ages.items():
                if not isinstance(brackets, dict):
                    continue
                for bracket, metrics in brackets.items():
                    if not isinstance(metrics, dict):
                        continue
                    for metric, val in metrics.items():
                        if not isinstance(val, (int, float)):
                            continue
                        rows.append({
                            "source": source,
                            "table_name": tbl_nm,
                            "period": str(year),
                            "dims": {"age": str(age), "bracket": str(bracket)},
                            "metric": str(metric),
                            "value": float(val),
                            "unit": "천원" if metric == "진료비" else "건",
                            "collected_at": collected,
                            "snapshot_id": snap,
                            "license": license_,
                        })
    elif kind == "surgery":
        for year, ages in cells.items():
            if not isinstance(ages, dict):
                continue
            for age, sexes in ages.items():
                if not isinstance(sexes, dict):
                    continue
                for sex, surgs in sexes.items():
                    if not isinstance(surgs, dict):
                        continue
                    for surg, metrics in surgs.items():
                        if not isinstance(metrics, dict):
                            continue
                        for metric, val in metrics.items():
                            if not isinstance(val, (int, float)):
                                continue
                            rows.append({
                                "source": source,
                                "table_name": tbl_nm,
                                "period": str(year),
                                "dims": {
                                    "age": str(age),
                                    "sex": str(sex),
                                    "surgery": str(surg),
                                },
                                "metric": str(metric),
                                "value": float(val),
                                "unit": unit_default,
                                "collected_at": collected,
                                "snapshot_id": snap,
                                "license": license_,
                            })
    elif kind == "disease_inout_age":
        # cells[year][disease][inout][age][metric]
        for year, diseases in cells.items():
            if not isinstance(diseases, dict):
                continue
            for disease, inouts in diseases.items():
                if not isinstance(inouts, dict):
                    continue
                for inout, ages in inouts.items():
                    if not isinstance(ages, dict):
                        continue
                    for age, metrics in ages.items():
                        if not isinstance(metrics, dict):
                            continue
                        for metric, val in metrics.items():
                            if not isinstance(val, (int, float)):
                                continue
                            unit = unit_default
                            if metric in ("진료비", "급여비"):
                                unit = "천원"
                            elif metric in ("내원일수", "급여일수"):
                                unit = "일"
                            elif metric == "진료실인원":
                                unit = "명"
                            rows.append({
                                "source": source,
                                "table_name": tbl_nm,
                                "period": str(year),
                                "dims": {
                                    "disease": str(disease),
                                    "inout": str(inout),
                                    "age": str(age),
                                },
                                "metric": str(metric),
                                "value": float(val),
                                "unit": unit,
                                "collected_at": collected,
                                "snapshot_id": snap,
                                "license": license_,
                            })
    else:
        logger.warning("stat_store: unknown layout for %s", tbl_id)
        return 0

    return upsert_rows(tbl_id, rows)


def latest_period(table_id: str) -> str | None:
    _ensure_engine()
    assert _Session is not None
    with _Session() as session:
        row = session.execute(
            select(StatFact.period)
            .where(StatFact.table_id == table_id)
            .order_by(StatFact.period.desc())
            .limit(1)
        ).first()
    return str(row[0]) if row else None


def get_stat(
        table_id: str,
        metric: str,
        dims: dict[str, str] | None = None,
        period: str = "latest") -> StatResult | None:
    """Exact key lookup. period='latest' uses max period string for the table."""
    _ensure_engine()
    assert _Session is not None
    per = period
    if per == "latest":
        per = latest_period(table_id) or ""
        if not per:
            return None
    ck = _cache_key(table_id, metric, dims, per)
    if ck in _mem:
        return _mem[ck]

    dims_json = _dims_key(dims)
    with _Session() as session:
        row = session.execute(
            select(StatFact).where(
                StatFact.table_id == table_id,
                StatFact.metric == metric,
                StatFact.period == per,
                StatFact.dims == dims_json,
            ).limit(1)
        ).scalar_one_or_none()
    if row is None:
        _mem[ck] = None
        return None
    result = StatResult(
        value=float(row.value),
        unit=row.unit or "",
        period=row.period,
        source=row.source or "",
        table_id=row.table_id,
        table_name=row.table_name or "",
        collected_at=row.collected_at or "",
        dims=json.loads(row.dims or "{}"),
        metric=row.metric,
    )
    _mem[ck] = result
    return result


def load_all_caches_into_db(cache_dir: Path | None = None) -> dict[str, int]:
    """Load every */latest.json under kosis_cache into SQLite."""
    from harag.guidance.kosis_nhis import CACHE_DIR, load_table_cache

    root = cache_dir or CACHE_DIR
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        tbl = child.name
        payload = load_table_cache(tbl)
        if not payload:
            continue
        try:
            n = upsert_from_snapshot(payload)
            out[tbl] = n
        except Exception as e:  # noqa: BLE001
            logger.warning("load %s failed: %s", tbl, e)
            out[tbl] = -1
    return out
