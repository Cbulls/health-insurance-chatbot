"""
품질 측정 하니스 — '어떻게 측정하는가'의 코드 구현.

기존 eval/harness.py가 '무엇을 측정'(유형별 recall·faithfulness)이라면,
이 모듈은 '어떻게 신뢰성 있게 측정'한다:
  - 부트스트랩 신뢰구간: 단일 점수가 아니라 [하한, 상한]을 보고한다.
    골드셋이 작으면 점수가 흔들리므로, "0.87"이 아니라 "0.87 [0.82, 0.91]"이라야
    SLO 통과 판정이 우연인지 실력인지 구분된다.
  - 통계적 게이트: SLO 통과 판정을 점추정이 아니라 신뢰구간 하한으로 한다
    (하한이 SLO를 넘어야 통과 — 보수적, 거짓 통과 방지).
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class MetricResult:
    """단일 점수가 아니라 신뢰구간과 함께 보고."""
    name: str
    point: float            # 점추정
    ci_low: float           # 신뢰구간 하한
    ci_high: float          # 신뢰구간 상한
    n: int                  # 표본 수

    def passes(self, slo: float) -> bool:
        """통과 판정: 신뢰구간 하한이 SLO를 넘어야 한다(보수적).
        점추정만 보면 작은 표본에서 우연히 통과할 수 있다."""
        return self.ci_low >= slo

    def __str__(self):
        return f"{self.name}={self.point:.3f} [{self.ci_low:.3f}, {self.ci_high:.3f}] (n={self.n})"


def bootstrap_ci(scores: list[float], confidence: float = 0.95,
                 iterations: int = 2000, seed: int = 42) -> tuple[float, float, float]:
    """부트스트랩으로 평균의 신뢰구간 추정.
    표본을 복원추출로 재샘플링해 평균 분포를 만들고, 분위수로 CI를 잡는다.
    골드셋이 작아도(100~200) 점수의 불확실성을 정량화할 수 있다."""
    if not scores:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(scores)
    means = []
    for _ in range(iterations):
        resample = [scores[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    point = sum(scores) / n
    lo_idx = int((1 - confidence) / 2 * iterations)
    hi_idx = int((1 + confidence) / 2 * iterations)
    return (point, means[lo_idx], means[min(hi_idx, iterations - 1)])


def measure(name: str, per_query_scores: list[float]) -> MetricResult:
    """질의별 점수 리스트 → 신뢰구간 포함 결과."""
    point, lo, hi = bootstrap_ci(per_query_scores)
    return MetricResult(name=name, point=point, ci_low=lo, ci_high=hi,
                        n=len(per_query_scores))


# ── retrieval 지표(질의별 0/1 또는 비율) ──
def recall_at_k_scores(gold_sets: list[set[str]], retrieved: list[list[str]],
                       k: int) -> list[float]:
    """질의별 recall@k. 부재 질의(gold 빈 집합)는 제외."""
    out = []
    for gold, ret in zip(gold_sets, retrieved):
        if not gold:
            continue
        hit = len(set(ret[:k]) & gold) / len(gold)
        out.append(hit)
    return out


def reciprocal_rank_scores(gold_sets: list[set[str]],
                           retrieved: list[list[str]]) -> list[float]:
    """질의별 RR(MRR의 질의 단위). 첫 정답의 역순위."""
    out = []
    for gold, ret in zip(gold_sets, retrieved):
        if not gold:
            continue
        rr = 0.0
        for rank, cid in enumerate(ret, 1):
            if cid in gold:
                rr = 1.0 / rank
                break
        out.append(rr)
    return out
