#!/usr/bin/env python3
"""
Gemini 무료 티어 일일 질문 예산 계산기.

AI Studio Usage에서 확인한 RPM/RPD를 넣고, harag 질문당 평균 콜 수를
반영해 RATE_LIMIT_QPM·일일 질문 상한을 출력한다.

  python scripts/gemini_quota_budget.py
  python scripts/gemini_quota_budget.py --rpm 10 --rpd 1500 --calls 2.5
"""
from __future__ import annotations

import argparse
import math


def recommend_qpm(rpm: float, calls_per_query: float, margin: int = 1) -> int:
    if rpm <= 0 or calls_per_query <= 0:
        return 0
    return max(1, int(math.floor(rpm / calls_per_query) - margin))


def daily_questions(rpd: float, calls_per_query: float, headroom: float = 0.7) -> int:
    if rpd <= 0 or calls_per_query <= 0:
        return 0
    return max(0, int(math.floor(rpd * headroom / calls_per_query)))


def main() -> int:
    p = argparse.ArgumentParser(description="Gemini quota → harag budget")
    p.add_argument("--rpm", type=float, default=10.0, help="Flash RPM (AI Studio)")
    p.add_argument("--rpd", type=float, default=1500.0, help="Flash RPD (AI Studio)")
    p.add_argument(
        "--calls", type=float, default=2.5,
        help="avg Gemini API calls per user question "
             "(1st turn≈2, multiturn rewrite on≈3)",
    )
    p.add_argument("--headroom", type=float, default=0.7, help="daily RPD safety factor")
    args = p.parse_args()

    qpm = recommend_qpm(args.rpm, args.calls)
    day = daily_questions(args.rpd, args.calls, args.headroom)

    print("=== Gemini → harag budget ===")
    print(f"Gemini RPM={args.rpm:g}  RPD={args.rpd:g}  calls/query={args.calls:g}")
    print(f"권장 RATE_LIMIT_QPM = {qpm}")
    print(f"권장 일일 질문 예산  ≈ {day}  (RPD×{args.headroom:g}÷calls)")
    print()
    print("시나리오 참고:")
    for label, c in [("첫 턴만(rewrite 없음)", 2.0),
                     ("멀티턴 rewrite on", 3.0),
                     ("rewrite off", 2.0)]:
        print(f"  {label}: QPM≤{recommend_qpm(args.rpm, c)}, "
              f"일일≈{daily_questions(args.rpd, c, args.headroom)}")
    print()
    print("실측 RPM/RPD는 AI Studio 프로젝트 Usage에서 확인 후 "
          "--rpm/--rpd 로 다시 돌리세요. 상세: docs/GEMINI_COST_STRATEGY.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
