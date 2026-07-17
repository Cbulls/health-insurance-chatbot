"""
도메인 골드셋 품질 게이트 — 오프라인(합성 output) 또는 라이브 평가.

  PYTHONPATH=src python -m harag.eval.run_quality_gate
  PYTHONPATH=src python -m harag.eval.run_quality_gate \\
      --goldset eval/goldsets/domain_health_insurance_v1.json \\
      --outputs eval/goldsets/domain_health_insurance_v1_outputs.json

종료코드: 0=통과, 1=SLO 위반, 2=입력 오류.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harag.eval.harness import (
    GoldQuery, QueryType, SystemOutput, QualitySLO, evaluate, ci_gate,
)
from harag.eval.quality import measure, recall_at_k_scores


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_goldset(path: Path) -> list[GoldQuery]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[GoldQuery] = []
    for q in data.get("queries", []):
        qtype = QueryType(q["qtype"])
        out.append(GoldQuery(
            qid=q["qid"],
            query=q["query"],
            qtype=qtype,
            gold_chunk_ids=set(q.get("gold_chunk_ids") or []),
            gold_answer=q.get("gold_answer"),
            source_document_id=q.get("source_document_id", ""),
            stale=bool(q.get("stale", False)),
            forbidden_claims=list(q.get("forbidden_claims") or []),
        ))
    return out


def load_outputs(path: Path) -> dict[str, SystemOutput]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("outputs") or data
    out: dict[str, SystemOutput] = {}
    for qid, o in raw.items():
        out[qid] = SystemOutput(
            retrieved_chunk_ids=list(o.get("retrieved_chunk_ids") or []),
            answer=o.get("answer"),
            cited_chunk_ids=list(o.get("cited_chunk_ids") or []),
        )
    return out


def default_slo() -> QualitySLO:
    return QualitySLO(
        min_hit_at_k=0.70,
        min_mrr=0.60,
        min_abstention_acc=0.90,
        min_citation_faithful=0.90,
    )


def run_gate(gold: list[GoldQuery], outputs: dict[str, SystemOutput],
             slo: QualitySLO | None = None) -> tuple[bool, list[str], dict]:
    missing = [g.qid for g in gold if g.qid not in outputs and not g.stale]
    if missing:
        return False, [f"missing outputs: {missing}"], {}

    reports = evaluate(gold, outputs, k=5)
    ok, violations = ci_gate(reports, slo or default_slo())

    # 부트스트랩 보조 리포트(차단은 아님)
    gold_sets = [g.gold_chunk_ids for g in gold
                 if g.qtype != QueryType.absent and not g.stale]
    retrieved = [outputs[g.qid].retrieved_chunk_ids for g in gold
                 if g.qtype != QueryType.absent and not g.stale]
    recall_scores = recall_at_k_scores(gold_sets, retrieved, k=5)
    extra = {}
    if recall_scores:
        extra["recall@5"] = str(measure("recall@5", recall_scores))

    summary = {
        "types": {k.value: {
            "n": v.n, "hit_k": v.hit_k, "mrr": v.mrr,
            "abstention_acc": v.abstention_acc,
            "citation_faithful": v.citation_faithful_rate,
            "answer_acc": v.answer_acc,
        } for k, v in reports.items()},
        "extra": extra,
        "violations": violations,
    }
    return ok, violations, summary


def main(argv: list[str] | None = None) -> int:
    root = _repo_root()
    p = argparse.ArgumentParser(description="Domain goldset quality gate")
    p.add_argument(
        "--goldset",
        type=Path,
        default=root / "eval/goldsets/domain_health_insurance_v1.json",
    )
    p.add_argument(
        "--outputs",
        type=Path,
        default=root / "eval/goldsets/domain_health_insurance_v1_outputs.json",
    )
    args = p.parse_args(argv)

    if not args.goldset.is_file():
        print(f"goldset not found: {args.goldset}", file=sys.stderr)
        return 2
    if not args.outputs.is_file():
        print(f"outputs not found: {args.outputs}", file=sys.stderr)
        return 2

    gold = load_goldset(args.goldset)
    outputs = load_outputs(args.outputs)
    ok, violations, summary = run_gate(gold, outputs)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if ok:
        print("QUALITY GATE PASS")
        return 0
    print("QUALITY GATE FAIL", file=sys.stderr)
    for v in violations:
        print(v, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
