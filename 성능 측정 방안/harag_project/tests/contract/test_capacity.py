"""국면 D 검증 — 용량 계산기가 제약 위반을 잡고 올바른 대응을 가리키는지."""
from harag.eval.capacity import (
    MeasuredLLM, MeasuredReranker, MeasuredRetrieval, WorkloadAssumptions,
    estimate_capacity,
)

PASS, FAIL = [], []
def ok(n, cond):
    (PASS if cond else FAIL).append((n, cond))


# ════════ 시나리오 1: 여유 있는 환경 ════════
llm_good = MeasuredLLM(gpu_tokens_per_sec=2000, max_concurrent_seqs=16, ttft_sec_p95=0.5)
rr = MeasuredReranker(latency_p99_at_k={30: 0.3, 50: 0.6})
ret = MeasuredRetrieval(search_p99_sec=0.05,
    ef_curve={64: (0.90, 0.02), 128: (0.96, 0.05), 256: (0.98, 0.11)})
wl_easy = WorkloadAssumptions(avg_output_tokens_ko=300, rerank_k=30,
    target_concurrent_users=50, e2e_latency_slo_sec=3.0, min_recall_at_k=0.95)

r1 = estimate_capacity(llm_good, rr, ret, wl_easy)
ok("S1/recall SLO 0.95 -> ef=128 chosen (min ef meeting recall)", r1.chosen_ef == 128)
ok("S1/recall at chosen ef >= target", r1.recall_at_chosen_ef >= 0.95)
ok("S1/latency SLO met", r1.latency_slo_met)
ok("S1/some GPUs needed", r1.llm_gpus_needed >= 1)
print(f"S1: GPU {r1.llm_gpus_needed}대, GPU당 동시성 {r1.llm_concurrency_per_gpu}, "
      f"체감지연 {sum(r1.e2e_latency_budget.values()):.2f}s, ef={r1.chosen_ef}")


# ════════ 시나리오 2: 한국어 토큰 팽창이 배칭 지연을 키움 ════════
# 같은 GPU·메모리인데 출력 토큰 3배 -> 최대 배칭 시 생성시간이 SLO를 위협
wl_korean = WorkloadAssumptions(avg_output_tokens_ko=900, rerank_k=30,
    target_concurrent_users=50, e2e_latency_slo_sec=3.0, min_recall_at_k=0.95)
r2 = estimate_capacity(llm_good, rr, ret, wl_korean)
# 동시성(메모리 지배)은 같지만, 토큰 3배라 배칭 시 생성시간이 길어져 경고
ok("S2/3x output tokens flags batching-latency risk",
   any("배칭" in n or "생성 시간" in n for n in r2.notes))
print(f"S2: 출력토큰 3배 -> {[n for n in r2.notes if '배칭' in n or '생성' in n]} "
      f"— 한국어 팽창이 지연에 직접 반영")


# ════════ 시나리오 3: recall SLO를 어떤 ef도 못 맞춤 ════════
ret_weak = MeasuredRetrieval(search_p99_sec=0.05,
    ef_curve={64: (0.80, 0.02), 128: (0.85, 0.05), 256: (0.88, 0.11)})
wl_strict = WorkloadAssumptions(avg_output_tokens_ko=300, rerank_k=30,
    target_concurrent_users=50, e2e_latency_slo_sec=3.0, min_recall_at_k=0.95)
r3 = estimate_capacity(llm_good, rr, ret_weak, wl_strict)
ok("S3/no ef meets recall -> chosen_ef None", r3.chosen_ef is None)
ok("S3/note flags recall problem (not silent)",
   any("recall" in n for n in r3.notes))
print(f"S3: recall 0.95 미달 -> {[n for n in r3.notes if 'recall' in n][0][:50]}...")


# ════════ 시나리오 4: 리랭커 K가 커서 지연 SLO 초과 ════════
rr_heavy = MeasuredReranker(latency_p99_at_k={30: 1.5, 50: 2.8})
wl_tight = WorkloadAssumptions(avg_output_tokens_ko=300, rerank_k=50,
    target_concurrent_users=50, e2e_latency_slo_sec=2.0, min_recall_at_k=0.90)
r4 = estimate_capacity(llm_good, rr_heavy, ret, wl_tight)
ok("S4/heavy reranker breaks latency SLO", not r4.latency_slo_met)
ok("S4/note suggests degradation (K reduction)",
   any("degradation" in n or "K 축소" in n for n in r4.notes))
print(f"S4: K=50 리랭커 지연 -> 체감 {sum(r4.e2e_latency_budget.values()):.2f}s "
      f"> SLO 2.0s -> graceful degradation 권고")


# ════════ 시나리오 5: 약한 GPU(메모리 작음) -> 동시성 < 1 경고 ════════
llm_weak = MeasuredLLM(gpu_tokens_per_sec=50, max_concurrent_seqs=1, ttft_sec_p95=2.0)
wl_heavy = WorkloadAssumptions(avg_output_tokens_ko=600, rerank_k=30,
    target_concurrent_users=20, e2e_latency_slo_sec=5.0, min_recall_at_k=0.90)
r5 = estimate_capacity(llm_weak, rr, ret, wl_heavy)
ok("S5/weak GPU forces many GPUs (low concurrency per GPU)",
   r5.llm_gpus_needed >= wl_heavy.target_concurrent_users)
print(f"S5: 약한 GPU -> GPU당 동시성 {r5.llm_concurrency_per_gpu}, "
      f"GPU {r5.llm_gpus_needed}대 필요(동시 사용자 {wl_heavy.target_concurrent_users}명)")


print(f"\n{'='*64}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for n, c in PASS: print("  OK", n)
for n, c in FAIL: print("  X ", n)
print('='*64)
print("\n핵심: 숫자는 자리표시자(측정 후 교체)지만, 계산기는 제약 위반을")
print("조용히 넘기지 않고 — recall 미달/SLO 초과/GPU 부족을 명시적으로 가리킨다.")
