"""
성능 측정 하니스 — '어떻게 측정하는가'의 코드 구현.

성능 측정의 함정과 대응:
  - 워밍업 오염: 첫 요청들은 JIT·캐시·커넥션풀 미초기화로 느리다.
    → 워밍업 구간을 측정에서 제외한다.
  - 평균의 거짓말: 평균은 꼬리 지연을 숨긴다. → p50/p95/p99 백분위수로 본다.
  - 단계 혼합: end-to-end만 보면 병목을 모른다. → 단계별 분해 측정.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LatencySample:
    """단일 요청의 단계별 지연(초)."""
    total: float
    stages: dict[str, float] = field(default_factory=dict)  # embedding/retrieval/rerank/ttft


@dataclass
class LatencyReport:
    n: int
    p50: float
    p95: float
    p99: float
    stage_p95: dict[str, float]

    def passes(self, slo_p95: float) -> bool:
        return self.p95 <= slo_p95

    def __str__(self):
        s = f"n={self.n} p50={self.p50*1000:.0f}ms p95={self.p95*1000:.0f}ms p99={self.p99*1000:.0f}ms"
        if self.stage_p95:
            stages = ", ".join(f"{k}={v*1000:.0f}ms" for k, v in self.stage_p95.items())
            s += f"\n  단계별 p95: {stages}"
        return s


def percentile(sorted_vals: list[float], p: float) -> float:
    """선형보간 백분위수. p in [0,100]."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (p / 100) * (len(sorted_vals) - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[lo]
    return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])


def analyze(samples: list[LatencySample], warmup: int = 0) -> LatencyReport:
    """워밍업 구간을 제외하고 백분위수·단계별 분해를 계산.

    warmup: 앞에서 제외할 요청 수(워밍업 오염 제거).
    """
    measured = samples[warmup:]
    if not measured:
        return LatencyReport(0, 0, 0, 0, {})

    totals = sorted(s.total for s in measured)
    # 단계별 p95
    stage_names = set()
    for s in measured:
        stage_names.update(s.stages.keys())
    stage_p95 = {}
    for name in stage_names:
        vals = sorted(s.stages.get(name, 0.0) for s in measured)
        stage_p95[name] = percentile(vals, 95)

    return LatencyReport(
        n=len(measured),
        p50=percentile(totals, 50),
        p95=percentile(totals, 95),
        p99=percentile(totals, 99),
        stage_p95=stage_p95,
    )
