"""오프라인 도메인 골드셋 품질 게이트."""
from __future__ import annotations

from pathlib import Path

from harag.eval.run_quality_gate import load_goldset, load_outputs, run_gate


def test_domain_goldset_gate_passes():
    root = Path(__file__).resolve().parents[2]
    gold = load_goldset(root / "eval/goldsets/domain_health_insurance_v1.json")
    outputs = load_outputs(
        root / "eval/goldsets/domain_health_insurance_v1_outputs.json")
    assert len(gold) >= 20
    ok, violations, summary = run_gate(gold, outputs)
    assert ok, violations
    assert "absent" in summary["types"]
