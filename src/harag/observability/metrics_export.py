"""
프로세스 로컬 MetricsCollector → Prometheus text exposition.
"""
from __future__ import annotations

import threading
from harag.observability.tracing import MetricsCollector

_lock = threading.Lock()
_collector = MetricsCollector()
_egress_tokens = 0
_stage_ms: dict[str, list[float]] = {}


def collector() -> MetricsCollector:
    return _collector


def record_query(abstained: bool, top_score: float | None = None,
                 stages: dict[str, float] | None = None,
                 egress_tokens: int = 0) -> None:
    global _egress_tokens
    with _lock:
        _collector.record_query(abstained)
        if top_score is not None:
            _collector.record_top_score(top_score)
        if egress_tokens:
            _egress_tokens += int(egress_tokens)
        if stages:
            for name, sec in stages.items():
                _stage_ms.setdefault(name, []).append(sec * 1000.0)


def prometheus_text() -> str:
    with _lock:
        total = _collector._total
        abstained = _collector._abstained
        rate = _collector.abstention_rate()
        p50 = _collector.score_p50()
        egress = _egress_tokens
        stages_snap = {k: list(v) for k, v in _stage_ms.items()}

    lines = [
        "# HELP harag_queries_total Total queries",
        "# TYPE harag_queries_total counter",
        f"harag_queries_total {total}",
        "# HELP harag_queries_abstained_total Abstained queries",
        "# TYPE harag_queries_abstained_total counter",
        f"harag_queries_abstained_total {abstained}",
        "# HELP harag_abstention_rate Abstention rate",
        "# TYPE harag_abstention_rate gauge",
        f"harag_abstention_rate {rate:.6f}",
        "# HELP harag_top_score_p50 Median top retrieval score",
        "# TYPE harag_top_score_p50 gauge",
        f"harag_top_score_p50 {p50:.6f}",
        "# HELP harag_llm_egress_tokens_total Estimated LLM egress tokens",
        "# TYPE harag_llm_egress_tokens_total counter",
        f"harag_llm_egress_tokens_total {egress}",
    ]
    for name, vals in stages_snap.items():
        if not vals:
            continue
        s = sorted(vals)
        p95 = s[int(0.95 * (len(s) - 1))] if len(s) > 1 else s[0]
        safe = name.replace("-", "_")
        lines.append(f"# HELP harag_stage_{safe}_p95_ms Stage p95 latency")
        lines.append(f"# TYPE harag_stage_{safe}_p95_ms gauge")
        lines.append(f"harag_stage_{safe}_p95_ms {p95:.3f}")
    return "\n".join(lines) + "\n"


def reset_for_tests() -> None:
    global _collector, _egress_tokens, _stage_ms
    with _lock:
        _collector = MetricsCollector()
        _egress_tokens = 0
        _stage_ms = {}
