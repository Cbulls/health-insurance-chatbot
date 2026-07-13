"""
B-3 동시성 계약 — 멱등 upsert without 빈 창.

문제: 순진한 'DELETE then INSERT'는 두 연산 사이에 빈 창을 만든다.
      그 창에 검색하면 문서가 존재하는데 0건 회수(조용한 실패, 환각보다 추적 어려움).
      하이브리드면 dense/sparse 인덱스 간 불일치 창도 생긴다.

해법: 버전 태깅 + 원자적 포인터 전환(문서 단위 blue-green).
  1. 새 버전을 다른 version 태그로 양쪽 인덱스에 전부 삽입(옛 버전 그대로 둠)
  2. '활성 버전' 포인터를 원자적으로 전환(단일 지점)
  3. 옛 버전은 비동기 GC로 청소
검색은 항상 활성 버전 필터를 달고 돈다 → 전환 전엔 옛 버전 온전히, 후엔 새 버전 온전히.
빈 창도 중복 창도 없다.

이 계약이 보장하는 불변식:
  C1. 어느 시점에 검색해도, 문서의 '정확히 한 버전'이 온전히 보인다(0건 창 없음).
  C2. 포인터 전환은 원자적(중간 상태 비가시).
  C3. dense/sparse는 같은 활성 버전을 가리킨다(쓰기 경로 인덱스 일관성).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class VersionedStore:
    """버전 태깅 인덱스의 최소 모델. 실제 VectorDB의 의미를 시뮬레이션.
    dense/sparse 두 인덱스를 같은 활성 포인터로 묶는 게 핵심."""
    # (document_id, version) -> 청크 id 집합. dense/sparse 각각.
    _dense: dict[tuple[str, int], set[str]] = field(default_factory=dict)
    _sparse: dict[tuple[str, int], set[str]] = field(default_factory=dict)
    # document_id -> 활성 버전 번호(이 포인터 전환이 원자적 단일 지점)
    _active: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def stage(self, doc_id: str, version: int, chunk_ids: set[str]) -> None:
        """1단계: 새 버전을 양쪽 인덱스에 삽입(아직 비활성). 검색에 안 보임."""
        self._dense[(doc_id, version)] = set(chunk_ids)
        self._sparse[(doc_id, version)] = set(chunk_ids)

    def activate(self, doc_id: str, version: int) -> None:
        """2단계: 활성 포인터 원자적 전환(C2). lock으로 단일 지점 보장."""
        with self._lock:
            # C3: 전환 전 양쪽 인덱스에 이 버전이 준비됐는지 확인(반쪽 활성화 금지)
            if (doc_id, version) not in self._dense or (doc_id, version) not in self._sparse:
                raise RuntimeError("cannot activate: version not staged in both indices (C3)")
            self._active[doc_id] = version

    def gc(self, doc_id: str, keep_version: int) -> None:
        """3단계: 비동기 청소. 활성 버전 외 제거. 활성 버전은 절대 못 지운다."""
        with self._lock:
            active = self._active.get(doc_id)
            if keep_version != active:
                raise RuntimeError("gc keep_version must equal active version (safety)")
            for store in (self._dense, self._sparse):
                for (d, v) in [k for k in store if k[0] == doc_id and v_ne(v, keep_version)]:
                    del store[(d, v)]

    def search_dense(self, doc_id: str) -> set[str]:
        """검색은 항상 활성 버전만 본다(C1)."""
        with self._lock:
            v = self._active.get(doc_id)
            if v is None:
                return set()
            return set(self._dense.get((doc_id, v), set()))

    def search_sparse(self, doc_id: str) -> set[str]:
        with self._lock:
            v = self._active.get(doc_id)
            if v is None:
                return set()
            return set(self._sparse.get((doc_id, v), set()))


def v_ne(v, keep):
    return v != keep


def upsert(store: VersionedStore, doc_id: str, new_version: int, new_chunks: set[str]) -> None:
    """멱등 upsert: stage -> activate. (gc는 별도 비동기)
    이 순서가 빈 창을 없앤다 — activate 전까지 검색은 옛 활성 버전을 온전히 본다."""
    store.stage(doc_id, new_version, new_chunks)   # 옛 버전 건드리지 않음
    store.activate(doc_id, new_version)            # 원자적 전환
