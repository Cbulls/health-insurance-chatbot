"""
임베딩 어댑터 구현.

  - ApiEmbeddingModel: OpenAI 호환 /embeddings 호출(httpx). 운영 경로.
  - LocalHashEmbeddingModel: 키 없이 도는 결정적 해시 임베딩. 오프라인 데모/스모크용.
  - SimpleMorph: 공백/어절 기반 토크나이저(sparse 자리채움; 실제 형태소는 Phase 2 Kiwi).

쿼리/문서가 반드시 같은 모델·차원을 쓰도록 build_embedding_model 하나로 통일한다
(과거 qdrant_store._dense_of의 더미 규칙 같은 불일치 방지).
"""
from __future__ import annotations

import math
import re

import httpx

from harag.config.settings import Settings

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


class SimpleMorph:
    """MVP 형태소 자리채움: 어절 + 문자 bigram. Phase 2에서 Kiwi로 교체."""

    def tokens(self, text: str) -> list[str]:
        return _TOKEN.findall(text.lower())


class LocalHashEmbeddingModel:
    """결정적 해시 임베딩(bag-of-words + 문자 bigram). 키 불필요.

    같은 토큰은 항상 같은 버킷 → 프로세스 간 일관(운영 sparse 해시 버그와 무관하게
    dense 폴백은 결정적). 어휘 겹침 기반 코사인이라 키워드 질의 데모가 실제로 동작한다.
    """

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_id = f"local-hash-{dim}"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat in self._features(text):
            h = hash_str(feat) % self.dim
            vec[h] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    @staticmethod
    def _features(text: str) -> list[str]:
        toks = _TOKEN.findall(text.lower())
        feats = list(toks)
        for t in toks:
            # 한국어는 조사로 어절이 흔들려 문자 bigram이 recall에 도움
            for i in range(len(t) - 1):
                feats.append(t[i:i + 2])
        return feats


def hash_str(s: str) -> int:
    """결정적 문자열 해시(파이썬 내장 hash의 프로세스별 랜덤화 회피)."""
    import hashlib
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


class ApiEmbeddingModel:
    """OpenAI 호환 임베딩 API 어댑터."""

    def __init__(self, api_base: str, api_key: str, model: str, dim: int,
                 timeout: float = 30.0, batch_size: int = 64):
        self._base = api_base.rstrip("/")
        self._key = api_key
        self.model_id = model
        self.dim = dim
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        # 커넥션 재사용(keep-alive) — 문서마다 새 TCP/TLS 핸드셰이크 방지.
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=timeout,
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 전체 텍스트를 한 요청에 넣으면 대형 문서에서 타임아웃/페이로드 한계로
        # 실패한다. batch_size로 나눠 호출하고 입력 순서를 보존해 이어붙인다.
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start:start + self._batch_size]
            out.extend(self._encode_batch(batch))
        return out

    def _encode_batch(self, batch: list[str]) -> list[list[float]]:
        resp = self._client.post(
            "/embeddings",
            json={"model": self.model_id, "input": batch},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # index 순서 보장
        data.sort(key=lambda d: d["index"])
        vecs = [d["embedding"] for d in data]
        for v in vecs:
            if len(v) != self.dim:
                raise ValueError(
                    f"embedding dim mismatch: got {len(v)}, expected {self.dim} "
                    f"(EMBEDDING_DIM 설정 또는 컬렉션 재생성 필요)")
        return vecs


def build_embedding_model(settings: Settings):
    """설정에 따라 임베딩 모델 생성(운영=API, 폴백=로컬 해시)."""
    if settings.embedding_provider == "openai" and settings.embedding_api_key:
        return ApiEmbeddingModel(
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )
    return LocalHashEmbeddingModel(dim=settings.embedding_dim)
