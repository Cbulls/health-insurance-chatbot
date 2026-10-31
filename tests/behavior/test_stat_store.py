"""stat_fact SQLite + occupation / A047 / A054 라벨."""
from __future__ import annotations

import os

import pytest

from harag.guidance.kosis_nhis import (
    TBL_A047,
    TBL_A054,
    TBL_A0611,
    TBL_N034,
    care_context_for,
    cost_bracket_context_for,
    disease_298_context_for,
    ensure_stat_db_from_caches,
    load_table_cache,
)
from harag.guidance.occupation_stats import MolKosisOccupationStats, default_occupation_stats
from harag.guidance.stat_store import clear_memory_cache, get_stat


@pytest.fixture(scope="module", autouse=True)
def _load_stats_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("stats") / "guidance_stats.db"
    os.environ["GUIDANCE_STATS_DSN"] = f"sqlite:///{db}"
    # reset module engine
    import harag.guidance.stat_store as ss
    ss._engine = None
    ss._Session = None
    clear_memory_cache()
    ensure_stat_db_from_caches()
    yield
    clear_memory_cache()
    ss._engine = None
    ss._Session = None


def test_cache_files_for_new_tables():
    assert load_table_cache(TBL_N034)
    assert load_table_cache(TBL_A047)
    assert load_table_cache(TBL_A054)
    assert load_table_cache(TBL_A0611)


def test_get_stat_n034_industry_total():
    row = get_stat(
        TBL_N034, "count",
        {"industry": "총계", "size": "전체"},
        period="latest",
    )
    assert row is not None
    assert row.value > 0
    assert "고용노동부" in row.source


def test_occupation_field_approx():
    ev = MolKosisOccupationStats().lookup("musculoskeletal_motion", "field")
    assert ev.missing is False
    assert ev.claims_not_incidence is True
    assert "운수" in ev.text or "건설" in ev.text
    assert "근사" in (ev.text or "")
    assert ev.source and "고용노동부" in ev.source


def test_occupation_student_missing():
    ev = default_occupation_stats().lookup("x", "student")
    assert ev.missing is True
    assert "산재" in (ev.reason or "")
    assert "학생" in (ev.reason or "")


def test_occupation_agriculture_approx():
    ev = default_occupation_stats().lookup("x", "agriculture")
    if not ev.missing:
        assert "농업" in (ev.text or "")
        assert ev.claims_not_incidence is True


def test_care_inpatient_a047():
    ctx = care_context_for("40-49", "female")
    assert ctx["missing"] is False
    inp = ctx.get("inpatient") or {}
    assert inp.get("missing") is False
    assert inp.get("tbl_id") == TBL_A047
    assert "입원" in (inp.get("detail") or "") + (inp.get("label") or "")


def test_cost_bracket_a054():
    ctx = cost_bracket_context_for("30-39")
    assert ctx["missing"] is False
    assert ctx.get("tbl_id") == TBL_A054
    assert ctx.get("year")
    assert "고액" in (ctx.get("label") or "")
    assert "확률" not in (ctx.get("note") or "")


def test_disease_298_head_neuro():
    ctx = disease_298_context_for("head_neuro", "30-39")
    assert ctx["missing"] is False
    assert ctx.get("tbl_id") == TBL_A0611
    assert "편두통" in (ctx.get("disease") or "")
    assert ctx.get("year")
    assert "유병" not in (ctx.get("note") or "") or "유병이 아닙니다" in (ctx.get("note") or "")
