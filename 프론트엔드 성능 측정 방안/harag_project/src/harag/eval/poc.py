"""
국면 A — HWP 파싱 PoC 측정 하니스.

목적: 표본만 넣으면 표 복원율을 실측하고 Go/No-Go를 판정한다.
이 환경엔 실제 행정문서 표본이 없으므로, 하니스(측정 방법론)를 완성하고
합성 표본으로 작동을 증명한다. 실제 표본 확보 시 그대로 돌린다.

보고서 측정 4원칙을 코드로 구현:
  1. 임계를 측정 전에 동결(굿하트 방어) — Settings에서 주입, 결과 보고 못 바꿈
  2. 층화 표집 — 포맷·연도·표 복잡도·구조 유형별로 최악 케이스 강제 포함
  3. 파싱 품질 × end-to-end를 곱셈 연결 — 천장 계산
  4. abstention 단독 거부권 — 다른 게 녹색이어도 abstention 실패면 NO-GO
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    GO = "GO"
    CONDITIONAL = "CONDITIONAL"
    NO_GO = "NO_GO"


@dataclass
class TableScore:
    """단일 문서의 표 복원 채점.
    cell_correct: 정답 표의 셀 중 (위치+값) 일치 비율.
    structure_correct: 행/열/병합 구조 일치 여부."""
    doc_id: str
    stratum: str                  # 층(예: "hwp5/2008/complex_table")
    cell_recovery: float          # 0~1
    structure_correct: bool
    parse_failed: bool = False    # 파싱 자체 실패(암호·손상)


@dataclass
class StratumResult:
    stratum: str
    n: int
    table_recovery_avg: float
    structure_ok_ratio: float
    parse_fail_ratio: float


@dataclass
class PoCReport:
    overall_recovery: float
    worst_stratum: StratumResult
    strata: list[StratumResult]
    end_to_end_ceiling: float     # 파싱 품질이 만드는 정확도 천장
    abstention_ok: bool
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)


@dataclass
class PoCThresholds:
    """측정 전에 동결되는 임계(굿하트 방어). Settings에서 주입."""
    min_table_recovery: float          # GO 하한
    conditional_table_recovery: float  # CONDITIONAL 하한(이 아래는 NO_GO)
    min_worst_stratum: float           # 최악 층 하한(꼬리 방어)
    required_end_to_end: float         # 사업 요구 정확도
    abstention_required: bool = True


def _stratify(scores: list[TableScore]) -> list[StratumResult]:
    by: dict[str, list[TableScore]] = {}
    for s in scores:
        by.setdefault(s.stratum, []).append(s)
    out = []
    for stratum, items in sorted(by.items()):
        n = len(items)
        rec = sum(i.cell_recovery for i in items) / n
        struct = sum(1 for i in items if i.structure_correct) / n
        fail = sum(1 for i in items if i.parse_failed) / n
        out.append(StratumResult(stratum, n, rec, struct, fail))
    return out


def evaluate_poc(scores: list[TableScore], th: PoCThresholds,
                 abstention_ok: bool) -> PoCReport:
    """표 채점 + 임계 → Go/No-Go 판정.

    판정 로직(원칙 적용):
      - 전체 평균이 아니라 '최악 층'으로도 본다(원칙 2: 꼬리 방어).
      - end-to-end 천장 = 전체 복원율(원칙 3: 곱셈 — 정확도는 파싱을 못 넘음).
      - abstention 실패는 단독 NO-GO 거부권(원칙 4).
    """
    strata = _stratify(scores)
    overall = sum(s.table_recovery_avg * s.n for s in strata) / sum(s.n for s in strata)
    worst = min(strata, key=lambda s: s.table_recovery_avg)
    ceiling = overall  # 파싱 품질이 end-to-end 정확도의 상한
    reasons: list[str] = []

    # 원칙 4: abstention 단독 거부권
    if th.abstention_required and not abstention_ok:
        return PoCReport(overall, worst, strata, ceiling, abstention_ok,
                         Verdict.NO_GO, ["abstention 실패 — 단독 거부권 발동"])

    # 원칙 3: 천장이 사업 요구를 못 받치면
    if ceiling < th.required_end_to_end:
        reasons.append(
            f"파싱 천장 {ceiling:.2f} < 사업 요구 정확도 {th.required_end_to_end:.2f}")

    # 전체 + 최악 층 동시 판정
    go = (overall >= th.min_table_recovery
          and worst.table_recovery_avg >= th.min_worst_stratum
          and not reasons)
    if go:
        verdict = Verdict.GO
    elif (overall >= th.conditional_table_recovery
          and worst.table_recovery_avg >= th.min_worst_stratum * 0.85):
        verdict = Verdict.CONDITIONAL
        reasons.append(
            f"전체 {overall:.2f} 또는 최악 층 {worst.table_recovery_avg:.2f}가 "
            f"GO 임계 미달 — 조건부(파서 개선·범위 조정 후 재측정)")
    else:
        verdict = Verdict.NO_GO
        reasons.append(
            f"전체 {overall:.2f} / 최악 층 {worst.table_recovery_avg:.2f} "
            f"< 조건부 임계 {th.conditional_table_recovery:.2f}")

    return PoCReport(overall, worst, strata, ceiling, abstention_ok, verdict, reasons)
