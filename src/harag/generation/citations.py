"""
구조화 인용 — 답변의 [문서 N] 마커를 실제 chunk_id로 매핑.

프롬프트가 컨텍스트를 [문서 1], [문서 2]…로 번호 매기고 LLM에게 근거 마커를
요구하므로, 답변의 마커 번호 N은 context_ids[N-1]에 1:1 대응한다.
범위 밖 번호(위조 마커)는 조용히 무시한다 — 매핑되지 않으므로 인용 목록에
오르지 못하고, 생성기의 위조 인용 가드(경계5)와도 충돌하지 않는다.
"""
from __future__ import annotations

import re

# "[문서 3]", "[문서3]", "[문서 1, 2]", "[문서 1][문서 2]" 모두 수용
_MARKER_RE = re.compile(r"\[\s*문서\s*((?:\d+\s*,?\s*)+)\]")


def extract_cited_ids(answer: str, context_ids: list[str]) -> list[str]:
    """답변 텍스트의 [문서 N] 마커를 chunk_id 목록으로(등장 순, 중복 제거)."""
    if not answer or not context_ids:
        return []
    cited: list[str] = []
    for m in _MARKER_RE.finditer(answer):
        for num in re.findall(r"\d+", m.group(1)):
            idx = int(num) - 1
            if 0 <= idx < len(context_ids):
                cid = context_ids[idx]
                if cid not in cited:
                    cited.append(cid)
    return cited
