"""국면 C 증명 — 유형별 분리 평가가 '평균에 가려진 약점'을 잡는지."""
from harag.eval.harness import (
    QueryType, GoldQuery, SystemOutput, evaluate, ci_gate, QualitySLO,
    ndcg_at_k, context_noise_rate,
)

PASS, FAIL = [], []
def ok(n, cond):
    (PASS if cond else FAIL).append((n, cond))

# ── 골드셋: 유형별 층화(적대적·부재 질의 포함) ──
gold = [
    GoldQuery("q1", "국내출장 여비 한도는?", QueryType.body, {"c1"}, "1일 5만원", "d1"),
    GoldQuery("q2", "출장비 정산 항목별 금액은?", QueryType.table, {"c2"}, "교통비 등", "d1"),
    GoldQuery("q3", "연차와 출장 둘 다 쓸 때 규정은?", QueryType.multi_clause, {"c3", "c4"}, "병행 가능", "d2"),
    # 적대적: 답이 문서에 없음 -> abstain이 정답
    GoldQuery("q4", "우주여행 출장비 한도는?", QueryType.absent, set(), None, "d1"),
    GoldQuery("q5", "재택근무 수당 한도는?", QueryType.absent, set(), None, "d1"),
]

SLO = QualitySLO(min_hit_at_k=0.8, min_mrr=0.6, min_abstention_acc=0.9, min_citation_faithful=0.95)


# ════════ 시나리오 1: 좋은 시스템(모든 유형 통과) ════════
good = {
    "q1": SystemOutput(["c1", "x"], "1일 5만원", ["c1"]),
    "q2": SystemOutput(["c2", "y"], "교통비 등", ["c2"]),
    "q3": SystemOutput(["c3", "c4"], "병행 가능", ["c3", "c4"]),
    "q4": SystemOutput(["z"], None),          # 올바르게 abstain
    "q5": SystemOutput(["w"], None),          # 올바르게 abstain
}
r_good = evaluate(gold, good, k=5)
passed_good, v_good = ci_gate(r_good, SLO)
ok("S1/good system passes CI gate", passed_good)


# ════════ 시나리오 2: '평균은 좋은데 abstention만 무너진' 시스템 ════════
# 이게 self-critique의 핵심 — 단일 평균이면 통과해버리는 위험한 시스템.
# 답하는 유형(q1~q3)은 완벽. 하지만 부재 질의(q4,q5)에 지어냄(abstain 실패).
sneaky = {
    "q1": SystemOutput(["c1"], "1일 5만원", ["c1"]),
    "q2": SystemOutput(["c2"], "교통비 등", ["c2"]),
    "q3": SystemOutput(["c3", "c4"], "병행 가능", ["c3", "c4"]),
    "q4": SystemOutput(["z"], "우주여행은 1일 100만원입니다"),   # 환각! abstain 했어야
    "q5": SystemOutput(["w"], "재택수당은 월 20만원입니다"),     # 환각!
}
r_sneaky = evaluate(gold, sneaky, k=5)

# 만약 단일 평균으로 봤다면? (대조: abstention을 전체에 섞어 평균)
all_outputs = sneaky
avg_abstention = sum(
    r.abstention_acc * r.n for r in r_sneaky.values()
) / sum(r.n for r in r_sneaky.values())
# body/table/multi는 답을 했으니 abstention_correct=1, absent 2개만 0
# -> 평균 = 3/5 = 0.6, 언뜻 '그럭저럭'으로 보임(위험한 착시)
ok("S2/single-average hides the failure (avg abstention ~0.6 looks okay-ish)",
   0.55 <= avg_abstention <= 0.65)

# 유형별 게이트는? absent 유형 abstention=0.0 -> 단독 거부권 발동
passed_sneaky, v_sneaky = ci_gate(r_sneaky, SLO)
ok("S2/type-separated gate BLOCKS the sneaky system", not passed_sneaky)
ok("S2/violation names absent abstention as the cause",
   any("absent" in v and "abstention" in v for v in v_sneaky))


# ════════ 시나리오 3: 표 유형만 약한 시스템(파싱 약점 노출) ════════
# 평균 retrieval은 높지만 table 유형만 검색 실패 -> 유형별이라 잡힘
weak_table = {
    "q1": SystemOutput(["c1"], "1일 5만원", ["c1"]),
    "q2": SystemOutput(["wrong1", "wrong2"], "모름주제", ["wrong1"]),  # 표 청크 회수 실패
    "q3": SystemOutput(["c3", "c4"], "병행 가능", ["c3", "c4"]),
    "q4": SystemOutput(["z"], None),
    "q5": SystemOutput(["w"], None),
}
r_weak = evaluate(gold, weak_table, k=5)
passed_weak, v_weak = ci_gate(r_weak, SLO)
ok("S3/table-only weakness blocked", not passed_weak)
ok("S3/violation points to table type",
   any("table" in v for v in v_weak))
ok("S3/body type still reported healthy", r_weak[QueryType.body].hit_k == 1.0)


# ════════ 시나리오 4: stale 질의 제외(갱신 거버넌스) ════════
gold_with_stale = gold + [
    GoldQuery("q6", "옛 규정 질의", QueryType.body, {"old"}, "옛답", "d1", stale=True),
]
r_stale = evaluate(gold_with_stale, {**good, "q6": SystemOutput(["x"], "틀린답")}, k=5)
# q6는 stale이라 평가에서 빠짐 -> body 유형 n은 여전히 1
ok("S4/stale query excluded from evaluation", r_stale[QueryType.body].n == 1)


# ════════ 시나리오 5: nDCG·컨텍스트 노이즈(리랭커 회귀) ════════
# gold=c1이 1등이면 nDCG=1, 3등이면 더 낮음. 노이즈는 top-k 비골드 비율.
perfect = ["c1", "x", "y"]
buried = ["x", "y", "c1"]
ok("S5/ndcg perfect rank = 1", ndcg_at_k({"c1"}, perfect, k=3) == 1.0)
ok("S5/ndcg buried < perfect",
   ndcg_at_k({"c1"}, buried, k=3) < ndcg_at_k({"c1"}, perfect, k=3))
ok("S5/noise lower when gold first",
   context_noise_rate({"c1"}, perfect, k=3)
   < context_noise_rate({"c1"}, ["x", "y", "z"], k=3))
ok("S5/evaluate reports ndcg for body",
   r_good[QueryType.body].ndcg_k == 1.0)


print(f"\n{'='*64}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for n, c in PASS: print("  OK", n)
for n, c in FAIL: print("  X ", n)
print('='*64)
print("\n핵심 증명: 단일 평균이면 통과했을 'abstention 붕괴 시스템'을")
print("유형별 게이트가 차단함. 평균은 약점을 가리고, 유형별은 약점을 가리킨다.")
