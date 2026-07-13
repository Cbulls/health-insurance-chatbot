"""
국면 D 용량 산정 계산기.

정직한 전제: 진짜 숫자는 이 환경에서 '측정'해야 나온다.
이 모듈은 '무엇을 측정해 어떻게 역산하는가'의 로직을 박는다.
입력(Measured*)은 측정으로 채워질 자리표시자. 로직(역산)은 실제로 작동한다.

핵심 식:
  지원 동시성 = GPU 처리량 / 요청당 부하
  필요 GPU 대수 = 목표 동시성 / GPU당 동시성 (올림)
  한국어 토큰 팽창은 LLM 생성 부하에 직접 곱해진다.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


# ════════ 측정으로 채워질 입력(자리표시자) ════════
@dataclass(frozen=True)
class MeasuredLLM:
    """LLM 서빙 측정치(연속 배칭 하). 한국어 토큰 기준으로 측정해야 함.

    동시성은 두 측정치로 갈린다(혼동 금지):
      - gpu_tokens_per_sec: 처리량 -> '초당 완료 요청' 결정
      - max_concurrent_seqs: KV캐시 메모리 -> '동시 진행 요청' 결정(배칭 폭)
    GPU당 실제 동시성은 이 둘 중 먼저 막히는 쪽이다."""
    gpu_tokens_per_sec: float       # GPU 1장의 총 생성 처리량(배칭 합산)
    max_concurrent_seqs: int        # KV캐시가 담는 동시 시퀀스 수(메모리 한계)
    ttft_sec_p95: float             # 첫 토큰까지(스트리밍 체감 지연)

@dataclass(frozen=True)
class MeasuredReranker:
    """K(후보 수)별 cross-encoder 지연. 꼬리 지연의 주범."""
    latency_p99_at_k: dict[int, float]   # {30: 0.4, 50: 0.7} 처럼

@dataclass(frozen=True)
class MeasuredRetrieval:
    """필터링 HNSW의 recall/지연 곡선(권한 분포 의존)."""
    search_p99_sec: float
    # ef 파라미터 -> (recall, 지연). recall↔지연 트레이드오프 곡선.
    ef_curve: dict[int, tuple[float, float]]   # {64:(0.92,0.02), 128:(0.97,0.05)}


@dataclass(frozen=True)
class WorkloadAssumptions:
    """워크로드 가정(도메인에서 합의)."""
    avg_output_tokens_ko: int       # 한국어 답변 평균 출력 토큰(팽창 반영된 실측)
    rerank_k: int                   # 리랭킹 후보 수
    target_concurrent_users: int    # 목표 동시 사용자
    e2e_latency_slo_sec: float      # end-to-end p99 SLO
    min_recall_at_k: float          # 검색 최소 recall(국면 C SLO와 연결)


# ════════ 역산 로직 ════════
@dataclass
class CapacityResult:
    llm_concurrency_per_gpu: float
    llm_gpus_needed: int
    e2e_latency_budget: dict[str, float]
    latency_slo_met: bool
    chosen_ef: int | None
    recall_at_chosen_ef: float | None
    notes: list[str]


def estimate_capacity(llm: MeasuredLLM, rr: MeasuredReranker, ret: MeasuredRetrieval,
                      wl: WorkloadAssumptions) -> CapacityResult:
    notes = []

    # ── LLM: GPU당 동시성은 두 한계 중 먼저 막히는 쪽 ──
    # 요청 1건의 평균 생성 시간(한국어 출력 토큰 / 처리량).
    gen_time_per_req = wl.avg_output_tokens_ko / llm.gpu_tokens_per_sec
    # (1) 메모리 한계: KV캐시가 담는 동시 시퀀스 수.
    conc_by_memory = float(llm.max_concurrent_seqs)
    # (2) 처리량 한계: 초당 완료 요청 수 × 요청 체류시간 = 리틀의 법칙(동시 진행 수).
    req_per_sec_per_gpu = llm.gpu_tokens_per_sec / wl.avg_output_tokens_ko
    conc_by_throughput = req_per_sec_per_gpu * gen_time_per_req  # = 1 시퀀스가 처리량 전부를 쓸 때
    # 실제 GPU당 동시성: 메모리가 담는 만큼 배칭하되, 그 배칭이 처리량을 나눠 쓴다.
    # 배칭 폭 = max_concurrent_seqs. 처리량은 그 폭으로 나뉘므로 요청당 느려지지만
    # 동시 진행 요청 수 자체는 메모리 한계가 지배한다.
    llm_conc_per_gpu = conc_by_memory
    llm_gpus = math.ceil(wl.target_concurrent_users / max(llm_conc_per_gpu, 1e-9))

    if llm_conc_per_gpu < 1:
        notes.append("LLM GPU당 동시성 < 1 — 요청 하나가 GPU를 사실상 독점. "
                     "양자화/작은 모델/더 많은 GPU 필요.")
    # 배칭이 커질수록 요청당 생성 시간이 늘어 지연 SLO를 위협(처리량↔지연 트레이드오프)
    effective_gen_time = gen_time_per_req * max(llm_conc_per_gpu, 1.0)
    if effective_gen_time > wl.e2e_latency_slo_sec:
        notes.append(f"최대 배칭 시 생성 시간 {effective_gen_time:.1f}s가 SLO 위협 — "
                     "배칭 폭 제한 필요(동시성↔지연 트레이드오프).")

    # ── 리랭킹: 선택한 K의 지연 ──
    rerank_lat = rr.latency_p99_at_k.get(wl.rerank_k)
    if rerank_lat is None:
        notes.append(f"rerank_k={wl.rerank_k} 측정치 없음 — 측정 필요.")
        rerank_lat = max(rr.latency_p99_at_k.values()) if rr.latency_p99_at_k else 0.0

    # ── 검색: recall SLO를 만족하는 최소 ef 선택(지연 최소화) ──
    chosen_ef, chosen_recall, chosen_search_lat = None, None, ret.search_p99_sec
    for ef in sorted(ret.ef_curve.keys()):
        recall, lat = ret.ef_curve[ef]
        if recall >= wl.min_recall_at_k:
            chosen_ef, chosen_recall, chosen_search_lat = ef, recall, lat
            break
    if chosen_ef is None:
        notes.append(f"어떤 ef도 recall {wl.min_recall_at_k} 미달 — "
                     "임베딩/청킹 개선 또는 recall SLO 재협상 필요.")

    # ── 단계별 지연 예산 합산 ──
    budget = {
        "embedding_query": 0.02,        # 자리표시자(측정 필요)
        "retrieval_hnsw": chosen_search_lat,
        "rerank": rerank_lat,
        "llm_ttft": llm.ttft_sec_p95,   # 체감 지연은 TTFT까지(스트리밍)
    }
    perceived_latency = sum(budget.values())  # 첫 토큰까지의 체감 지연
    slo_met = perceived_latency <= wl.e2e_latency_slo_sec
    if not slo_met:
        notes.append(f"체감 지연 {perceived_latency:.2f}s > SLO {wl.e2e_latency_slo_sec}s — "
                     "리랭커 K 축소 또는 단계 최적화 필요(graceful degradation).")

    return CapacityResult(
        llm_concurrency_per_gpu=round(llm_conc_per_gpu, 2),
        llm_gpus_needed=llm_gpus,
        e2e_latency_budget=budget,
        latency_slo_met=slo_met,
        chosen_ef=chosen_ef,
        recall_at_chosen_ef=chosen_recall,
        notes=notes,
    )
