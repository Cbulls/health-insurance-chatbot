"""
생성 출력 가드 — 동기 generate / 스트림 경로 공유.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from harag.security.injection import check_output_for_canary

_NO_EVIDENCE_RE = re.compile(
    r"(정보를 찾을 수 없|정보가 (없|나와\s*있지 않|포함되어 있지 않)"
    r"|기재되어 있지 않|관련 내용이 .{0,24}있지 않"
    r"|근거가 없|언급되어 있지 않|알 수 없습니다|모릅니다|모른다고?\b)")

# LLM이 「없다」고 했을 때 hard kill 대신 소비자용 soft 안내.
SOFT_NO_EVIDENCE_ANSWER = (
    "이 설명서에서 질문과 바로 맞는 구절을 찾지 못했습니다. "
    "날짜·일수 같은 확정 수치는 지어내지 않습니다.\n\n"
    "약관·상품요약서의 「보험기간」「책임개시」「면책」「대기」 관련 조항을 "
    "확인하시거나, 아래처럼 바꿔 물어보시면 찾기가 쉽습니다.\n"
    "· 책임개시일(보장 시작)은 언제인가요?\n"
    "· 해당 담보의 면책·대기기간은 며칠인가요?\n\n"
    "확정 안내가 필요하면 보험사 고객센터·설계사 상담을 함께 이용해 주세요."
)


@dataclass
class OutputGuardResult:
    ok: bool
    answer: str | None
    cited_chunk_ids: list[str]
    abstain_reason: str | None = None


class OutputGuard:
    """인용 위조·canary 유출 검사. no_evidence는 soft 안내로 치환."""

    def validate(
        self,
        answer: str | None,
        cited_chunk_ids: list[str],
        available_ids: list[str],
        canary: str | None = None,
    ) -> OutputGuardResult:
        if answer is None:
            return OutputGuardResult(
                ok=False, answer=None, cited_chunk_ids=[],
                abstain_reason="empty_answer")

        if check_output_for_canary(answer, canary):
            return OutputGuardResult(
                ok=False, answer=None, cited_chunk_ids=[],
                abstain_reason="injection_canary_leak")

        available = set(available_ids)
        fabricated = [c for c in cited_chunk_ids if c not in available]
        if fabricated:
            return OutputGuardResult(
                ok=False, answer=None, cited_chunk_ids=[],
                abstain_reason=f"fabricated_citation:{fabricated}")

        if _NO_EVIDENCE_RE.search(answer):
            # FAQ·공식 웹·보험료 안내 근거가 있으면 soft 치환하지 않음
            # (LLM이 「설명서에 없다」고 해도 공통 안내/사이트를 쓰게 둠).
            has_alt = any(
                (cid or "").startswith(("web-", "faq-", "premium-"))
                or cid in ("faq-context", "premium-context", "match-context")
                for cid in available_ids
            )
            if not has_alt:
                cites = list(available_ids)[:3]
                return OutputGuardResult(
                    ok=True,
                    answer=SOFT_NO_EVIDENCE_ANSWER,
                    cited_chunk_ids=cites,
                    abstain_reason=None,
                )

        # A6: 마커가 있으면 유효 인용만 유지. 없으면 검색 컨텍스트 앞쪽을
        # 자동 연결(본문에 [문서 N] 강제 삽입 없음 → 자연스러운 답 + 칩 출처).
        valid = [c for c in cited_chunk_ids if c in available]
        if not valid:
            if not available_ids:
                return OutputGuardResult(
                    ok=False, answer=None, cited_chunk_ids=[],
                    abstain_reason="missing_citation")
            fallback = list(available_ids)[:3]
            return OutputGuardResult(
                ok=True, answer=answer, cited_chunk_ids=fallback)

        return OutputGuardResult(
            ok=True, answer=answer, cited_chunk_ids=valid)
