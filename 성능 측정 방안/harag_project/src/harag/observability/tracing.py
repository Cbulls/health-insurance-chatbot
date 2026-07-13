"""
관측 — 단계별 trace + 메트릭 + 감사 로그(NFR-6).

QueryTrace: 한 질의의 단계별 지연·검색결과·외부유출을 하나의 trace_id로 묶는다.
MetricsCollector: abstention율·점수 분포를 집계(대시보드·알림용).
환각 신고가 들어오면 trace_id로 검색→생성 전 단계를 재구성한다.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable


class QueryTrace:
    def __init__(self, trace_id: str, query: str,
                 mask: Callable[[str], str] | None = None):
        self.trace_id = trace_id
        self._query = mask(query) if mask else query
        self._stages: dict[str, float] = {}
        self._retrieved: list[tuple[str, float]] = []
        self._egress: list[dict] = []

    @contextmanager
    def stage(self, name: str):
        """단계 실행 시간 측정. with tr.stage('retrieval'): ..."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self._stages[name] = time.perf_counter() - start

    def record_retrieval(self, chunk_scores: list[tuple[str, float]]):
        """검색된 청크 ID·점수 기록(OBS-01, 사후 재현)."""
        self._retrieved = list(chunk_scores)

    def record_egress(self, provider: str, token_estimate: int):
        """외부 API로 나간 트래픽 감사(OBS-04, 네트워크 경계)."""
        self._egress.append({"provider": provider, "tokens": token_estimate})

    def snapshot(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "query": self._query,
            "stages": dict(self._stages),
            "retrieved": list(self._retrieved),
            "egress": list(self._egress),
        }


class MetricsCollector:
    """집계 메트릭. 실제론 Prometheus 카운터/히스토그램으로 노출."""

    def __init__(self):
        self._total = 0
        self._abstained = 0
        self._top_scores: list[float] = []

    def record_query(self, abstained: bool):
        self._total += 1
        if abstained:
            self._abstained += 1

    def record_top_score(self, score: float):
        self._top_scores.append(score)

    def abstention_rate(self) -> float:
        return self._abstained / self._total if self._total else 0.0

    def score_p50(self) -> float:
        if not self._top_scores:
            return 0.0
        s = sorted(self._top_scores)
        return s[len(s) // 2]
