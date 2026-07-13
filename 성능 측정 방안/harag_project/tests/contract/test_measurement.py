"""품질·성능 측정 방법론 테스트 — 방법론이 의도대로 작동하는지 고정."""
from harag.eval.quality import measure, bootstrap_ci
from harag.eval.perf import LatencySample, analyze, percentile

PASS, FAIL = [], []
def ok(n, cond): (PASS if cond else FAIL).append(n)


# ── 품질: 신뢰구간 게이트가 작은 표본의 우연 통과를 막는다 ──
small = measure("r", [1.0]*8 + [0.0]*2)   # n=10, 점추정 0.8
ok("small sample: point passes 0.75", small.point >= 0.75)
ok("small sample: CI-lower gate BLOCKS 0.75 (보수적)", not small.passes(0.75))

large = measure("r", [1.0]*80 + [0.0]*20)  # n=100, 동일 0.8
ok("large sample: CI narrows (하한 상승)", large.ci_low > small.ci_low)

# 완벽한 점수는 표본 충분하면 통과
perfect = measure("r", [1.0]*100)
ok("perfect large sample passes", perfect.passes(0.95))


# ── 성능: 워밍업 제외가 꼬리 지연 오염을 제거한다 ──
samples = [LatencySample(total=3.0) for _ in range(5)]  # 워밍업(느림)
samples += [LatencySample(total=0.8) for _ in range(100)]  # 정상
r_polluted = analyze(samples, warmup=0)
r_clean = analyze(samples, warmup=5)
ok("warmup pollution inflates p99", r_polluted.p99 > r_clean.p99)
ok("clean p99 stable", r_clean.p99 < 1.0)

# 백분위수 계산 정확성
vals = sorted([float(i) for i in range(1, 101)])  # 1..100
ok("p50 ~ 50", abs(percentile(vals, 50) - 50.5) < 1.0)
ok("p95 ~ 95", abs(percentile(vals, 95) - 95.05) < 1.0)

# 단계별 분해
staged = [LatencySample(total=1.0, stages={"rerank": 0.5, "retrieval": 0.05})
          for _ in range(50)]
rep = analyze(staged, warmup=0)
ok("stage decomposition identifies bottleneck",
   rep.stage_p95["rerank"] > rep.stage_p95["retrieval"])


print(f"\n{'='*56}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for n in PASS: print("  OK", n)
for n in FAIL: print("  X ", n)
print('='*56)
