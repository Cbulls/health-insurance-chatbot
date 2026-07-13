"""국면 A PoC 측정 하니스 — 측정 4원칙이 작동하는지 고정."""
from harag.eval.poc import TableScore, PoCThresholds, evaluate_poc, Verdict

PASS, FAIL = [], []
def ok(n, c): (PASS if c else FAIL).append(n)

TH = PoCThresholds(min_table_recovery=0.85, conditional_table_recovery=0.70,
                   min_worst_stratum=0.75, required_end_to_end=0.80)

# 원칙 2: 평균은 통과해도 최악 층이 나쁘면 막는다
mixed = ([TableScore(f's{i}', 'simple', 0.95, True) for i in range(20)]
         + [TableScore(f'c{i}', 'complex', 0.55, False) for i in range(8)])
r = evaluate_poc(mixed, TH, abstention_ok=True)
ok("최악 층이 평균을 무시하고 판정에 반영", r.verdict != Verdict.GO)
ok("최악 층을 정확히 식별", r.worst_stratum.stratum == "complex")

# 원칙 4: abstention 단독 거부권
good = [TableScore(f'd{i}', 'simple', 0.92, True) for i in range(30)]
r_abs = evaluate_poc(good, TH, abstention_ok=False)
ok("abstention 실패 시 복원율 우수해도 NO_GO", r_abs.verdict == Verdict.NO_GO)
ok("거부권 사유 명시", "abstention" in r_abs.reasons[0])

# 정상: 전반 우수 -> GO
ok("전반 우수 + abstention OK -> GO",
   evaluate_poc(good, TH, abstention_ok=True).verdict == Verdict.GO)

# 원칙 3: 천장이 사업 요구 미달이면 GO 안 됨
mid = [TableScore(f'm{i}', 'simple', 0.78, True) for i in range(30)]
r_ceil = evaluate_poc(mid, TH, abstention_ok=True)
ok("파싱 천장이 사업 요구 미달이면 GO 차단", r_ceil.verdict != Verdict.GO)

# CONDITIONAL 구간 존재
cond = [TableScore(f'x{i}', 'simple', 0.80, True) for i in range(30)]
r_cond = evaluate_poc(cond, TH, abstention_ok=True)
ok("중간 구간은 CONDITIONAL", r_cond.verdict == Verdict.CONDITIONAL)

print(f"\n{'='*52}\nPASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for n in PASS: print("  OK", n)
for n in FAIL: print("  X ", n)
print('='*52)
