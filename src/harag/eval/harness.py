"""
국면 C 평가 하니스.

설계가 회피하지 않는 것(self-critique 3):
  - 골드 질의는 유형별로 층화되고, 점수는 유형별로 분리 측정(단일 평균 금지 = 굿하트 방어).
  - 적대적/부재 질의를 1급 유형으로 포함(LLM 생성의 자기회피 편향 차단).
  - retrieval 실패형과 generation 실패형을 절대 한 점수로 합치지 않음.
  - abstention(부재 질의에 '모른다')은 단독 거부권을 갖는 CI 게이트.
  - 골드 질의는 document_id에 연결 — 문서 개정 시 stale 표시(갱신 거버넌스).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class QueryType(str, Enum):
    """질의 유형 층화 — 유형별로 점수를 따로 본다."""
    body = "body"                  # 본문 단일 조항
    table = "table"                # 답이 표(병합셀)에 있음 — 파싱 약점 직격
    multi_clause = "multi_clause"  # 여러 조항 종합 — 청킹이 단서를 찢는지
    absent = "absent"              # 답이 문서에 없음 — abstention 검증(적대적)
    conflicting = "conflicting"    # 개정 전후 충돌 — 버전 전환(B-3) 검증


@dataclass
class GoldQuery:
    qid: str
    query: str
    qtype: QueryType
    # 근거 청크 id 집합(retrieval 채점용). absent면 빈 집합.
    gold_chunk_ids: set[str]
    # 정답 텍스트(correctness 채점용). absent면 None(=모른다가 정답).
    gold_answer: str | None
    # 갱신 거버넌스: 어떤 문서에서 왔는가. 그 문서 개정 시 stale.
    source_document_id: str
    stale: bool = False


@dataclass
class SystemOutput:
    """평가 대상 시스템의 한 질의 응답."""
    retrieved_chunk_ids: list[str]   # 검색 결과(순위 순)
    answer: str | None               # None이면 abstain(모른다)
    cited_chunk_ids: list[str] = field(default_factory=list)


# ════════ Retrieval 지표(생성 이전, 결정적) ════════
def hit_at_k(gold: set[str], retrieved: list[str], k: int) -> float:
    if not gold:
        return 1.0  # absent 질의는 retrieval 채점 제외(1.0 중립)
    return 1.0 if (set(retrieved[:k]) & gold) else 0.0

def recall_at_k(gold: set[str], retrieved: list[str], k: int) -> float:
    if not gold:
        return 1.0
    return len(set(retrieved[:k]) & gold) / len(gold)

def mrr(gold: set[str], retrieved: list[str]) -> float:
    if not gold:
        return 1.0
    for rank, cid in enumerate(retrieved, start=1):
        if cid in gold:
            return 1.0 / rank
    return 0.0


def _dcg_at_k(relevances: list[float], k: int) -> float:
    s = 0.0
    for i, rel in enumerate(relevances[:k]):
        # 표준 DCG: rel / log2(rank+1), rank는 1-based
        s += rel / math.log2(i + 2)
    return s


def ndcg_at_k(gold: set[str], retrieved: list[str], k: int = 5) -> float:
    """이진 관련성 nDCG@k — 리랭커 순위 품질(골드 in top-K일 때 상단 집중도)."""
    if not gold:
        return 1.0
    rels = [1.0 if cid in gold else 0.0 for cid in retrieved[:k]]
    dcg = _dcg_at_k(rels, k)
    ideal = _dcg_at_k(sorted(rels, reverse=True), k)
    # ideal이 0이면 gold가 top-k에 없음
    if ideal <= 0:
        # gold가 더 있을 수 있으므로 ideal은 min(|gold|, k)개의 1.0
        ideal = _dcg_at_k([1.0] * min(len(gold), k), k)
        if ideal <= 0:
            return 0.0
    return dcg / ideal


def context_noise_rate(gold: set[str], retrieved: list[str], k: int = 5) -> float:
    """top-k 중 비골드 비율 — 컨텍스트 노이즈(낮을수록 좋음). absent면 0."""
    if not gold:
        return 0.0
    top = retrieved[:k]
    if not top:
        return 1.0
    junk = sum(1 for cid in top if cid not in gold)
    return junk / len(top)


# ════════ Generation 지표 ════════
def is_abstention(out: SystemOutput) -> bool:
    return out.answer is None

def abstention_correct(gold: GoldQuery, out: SystemOutput) -> bool:
    """absent 질의엔 abstain이 정답. 그 외엔 답을 해야 정답."""
    if gold.qtype == QueryType.absent:
        return is_abstention(out)          # 모른다고 해야 맞음
    return not is_abstention(out)          # 답을 해야 맞음(틀린 답인지는 correctness가 따로 봄)

def citation_faithful(out: SystemOutput) -> bool:
    """인용이 검색 결과 안에 실재하는가(B-2 경계5의 평가판)."""
    if out.answer is None:
        return True
    return all(c in set(out.retrieved_chunk_ids) for c in out.cited_chunk_ids)


# ════════ 유형별 분리 집계(단일 평균 금지) ════════
@dataclass
class TypeReport:
    qtype: QueryType
    n: int
    hit_k: float
    mrr: float
    abstention_acc: float
    citation_faithful_rate: float
    ndcg_k: float = 0.0
    context_noise: float = 0.0


def evaluate(gold_set: list[GoldQuery],
             outputs: dict[str, SystemOutput],
             k: int = 5) -> dict[QueryType, TypeReport]:
    by_type: dict[QueryType, list[GoldQuery]] = {}
    for g in gold_set:
        if g.stale:
            continue  # stale 질의는 평가에서 제외(갱신 거버넌스)
        by_type.setdefault(g.qtype, []).append(g)

    reports = {}
    for qtype, qs in by_type.items():
        n = len(qs)
        hk = sum(hit_at_k(g.gold_chunk_ids, outputs[g.qid].retrieved_chunk_ids, k) for g in qs) / n
        mr = sum(mrr(g.gold_chunk_ids, outputs[g.qid].retrieved_chunk_ids) for g in qs) / n
        ab = sum(abstention_correct(g, outputs[g.qid]) for g in qs) / n
        cf = sum(citation_faithful(outputs[g.qid]) for g in qs) / n
        nd = sum(ndcg_at_k(g.gold_chunk_ids, outputs[g.qid].retrieved_chunk_ids, k) for g in qs) / n
        nz = sum(context_noise_rate(g.gold_chunk_ids, outputs[g.qid].retrieved_chunk_ids, k) for g in qs) / n
        reports[qtype] = TypeReport(qtype, n, hk, mr, ab, cf, nd, nz)
    return reports


# ════════ CI 게이트(유형별 + abstention 단독 거부권) ════════
@dataclass(frozen=True)
class QualitySLO:
    min_hit_at_k: float
    min_mrr: float
    min_abstention_acc: float       # absent 유형에 단독 적용
    min_citation_faithful: float


def ci_gate(reports: dict[QueryType, TypeReport], slo: QualitySLO) -> tuple[bool, list[str]]:
    """통과 여부 + 위반 사유. 유형별로 검사 — 전체 평균이 통과해도 한 유형이 무너지면 차단."""
    violations = []
    for qtype, r in reports.items():
        # absent 유형은 abstention이 핵심 지표(단독 거부권)
        if qtype == QueryType.absent:
            if r.abstention_acc < slo.min_abstention_acc:
                violations.append(
                    f"[BLOCK] {qtype.value}: abstention {r.abstention_acc:.2f} < {slo.min_abstention_acc} "
                    f"(틀린 답보다 위험 — 단독 거부권)")
            continue
        # 답하는 유형은 retrieval + 인용 충실도
        if r.hit_k < slo.min_hit_at_k:
            violations.append(f"[BLOCK] {qtype.value}: hit@k {r.hit_k:.2f} < {slo.min_hit_at_k}")
        if r.mrr < slo.min_mrr:
            violations.append(f"[BLOCK] {qtype.value}: mrr {r.mrr:.2f} < {slo.min_mrr}")
        if r.citation_faithful_rate < slo.min_citation_faithful:
            violations.append(f"[BLOCK] {qtype.value}: citation {r.citation_faithful_rate:.2f} < {slo.min_citation_faithful}")
    return (len(violations) == 0, violations)
