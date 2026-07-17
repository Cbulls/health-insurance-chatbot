"""
생성 출력 가드 — 동기 generate / 스트림 경로 공유.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from harag.security.injection import check_output_for_canary

_NO_EVIDENCE_RE = re.compile(
    r"(정보를 찾을 수 없|정보가 (없|나와\s*있지 않|포함되어 있지 않)"
    r"|근거가 없|언급되어 있지 않|알 수 없습니다|모릅니다|모른다고?\b)")


@dataclass
class OutputGuardResult:
    ok: bool
    answer: str | None
    cited_chunk_ids: list[str]
    abstain_reason: str | None = None


class OutputGuard:
    """인용 위조·no_evidence·canary 유출 검사."""

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
            return OutputGuardResult(
                ok=False, answer=None, cited_chunk_ids=[],
                abstain_reason="no_evidence_in_documents")

        # A6: 비기권 답변은 유효 인용 `[문서 N]` ≥1 필수
        valid = [c for c in cited_chunk_ids if c in available]
        if not valid:
            return OutputGuardResult(
                ok=False, answer=None, cited_chunk_ids=[],
                abstain_reason="missing_citation")

        return OutputGuardResult(
            ok=True, answer=answer, cited_chunk_ids=valid)
