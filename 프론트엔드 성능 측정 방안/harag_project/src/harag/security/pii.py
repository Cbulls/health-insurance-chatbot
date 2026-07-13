"""
PII 마스킹 — 한국 행정문서의 민감정보를 인덱싱 시점에 제거(SEC-03).

원칙: 적재 전에 마스킹한다. 그 뒤로 검색·LLM 컨텍스트·외부 API·로그 어디에도
원본 PII가 없다(v4 외부 경계 방어). 응답 시점 마스킹은 이미 늦다.

오탐 방지가 품질의 핵심: 조항번호·금액·일반 날짜를 PII로 오인하면
검색·답변이 망가진다. 그래서 한국 PII의 '구체적 형식'만 정밀하게 잡는다.

한계(정직): 정규식은 형식이 명확한 PII(주민번호·전화·계좌·이메일)에 강하지만,
문맥 의존 PII(이름·주소)는 못 잡는다. 그건 NER 모델이 필요(범위 밖, 향후).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# 순서 중요: 더 구체적·긴 패턴을 먼저(부분 매칭 방지)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 주민/외국인 등록번호: 6자리-7자리. 뒷자리 첫 숫자로 구분하지만 둘 다 PII.
    ("resident_number", re.compile(r"\b\d{6}-\d{7}\b")),
    # 이메일
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # 휴대폰: 010-XXXX-XXXX (지역번호와 구분 위해 010 한정)
    ("phone", re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b")),
    # 계좌번호: 2~6자리 그룹이 하이픈으로 3덩이 이상(은행 계좌 형식)
    ("account", re.compile(r"\b\d{2,6}-\d{2,6}-\d{4,6}\b")),
    # 여권번호: 영문1 + 숫자8
    ("passport", re.compile(r"\b[A-Z]\d{8}\b")),
]

# 마스킹 제외(오탐 방지): 조항번호 패턴은 PII와 형식이 겹치지 않지만,
# 혹시 계좌 패턴에 걸릴 수 있는 '제N조' 류를 보호하기 위한 가드.
_CLAUSE_GUARD = re.compile(r"제\s*\d+\s*조(?:의\s*\d+)?")


@dataclass
class PiiMasker:
    mask_token: str = "[PII]"

    def mask(self, text: str) -> tuple[str, dict[str, int]]:
        """text에서 PII를 마스킹. (마스킹된 텍스트, 종류별 건수) 반환."""
        report: dict[str, int] = {}

        # 조항번호 위치를 보호 구간으로 기록(그 안은 마스킹 금지)
        protected = [(m.start(), m.end()) for m in _CLAUSE_GUARD.finditer(text)]

        def in_protected(s: int, e: int) -> bool:
            return any(ps <= s and e <= pe for ps, pe in protected)

        result = text
        for name, pat in _PATTERNS:
            count = 0
            # 뒤에서부터 치환(인덱스 밀림 방지)
            for m in reversed(list(pat.finditer(result))):
                if in_protected(m.start(), m.end()):
                    continue
                result = result[:m.start()] + self.mask_token + result[m.end():]
                count += 1
            if count:
                report[name] = report.get(name, 0) + count
        return result, report
