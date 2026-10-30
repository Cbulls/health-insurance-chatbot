"""
임베딩 어댑터 구현.

  - ApiEmbeddingModel: OpenAI 호환 /embeddings 호출(httpx). 운영 경로.
  - LocalHashEmbeddingModel: 키 없이 도는 결정적 해시 임베딩. 오프라인 데모/스모크용.
  - SimpleMorph: 공백/어절 기반 토크나이저(sparse 자리채움; 실제 형태소는 Phase 2 Kiwi).

쿼리/문서가 반드시 같은 모델·차원을 쓰도록 build_embedding_model 하나로 통일한다
(과거 qdrant_store._dense_of의 더미 규칙 같은 불일치 방지).
"""
from __future__ import annotations

import logging
import math
import re
import time

import httpx

from harag.config.settings import Settings

logger = logging.getLogger("harag.embed")


class EmbeddingRateLimitError(Exception):
    """임베딩 API 429/5xx가 재시도 후에도 남음."""


def is_embedding_failure(exc: BaseException) -> bool:
    """Jina 잔액·429·403 등 dense 임베딩 API 실패 여부."""
    if isinstance(exc, EmbeddingRateLimitError):
        return True
    name = type(exc).__name__
    if "Embedding" in name and ("Rate" in name or "Error" in name):
        return True
    msg = str(exc).lower()
    return (
        "insufficient" in msg
        or "insufficient account balance" in msg
        or ("embedding" in msg and ("429" in msg or "403" in msg))
    )


_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


class SimpleMorph:
    """MVP 형태소 자리채움: 어절 + 문자 bigram. Phase 2에서 Kiwi로 교체."""

    def tokens(self, text: str) -> list[str]:
        return _TOKEN.findall(text.lower())


# sparse 신호에 유효한 품사: 명사(N*)·동사/형용사(V*)·어근(XR)·숫자(SN)·외국어(SL)
_KIWI_TAGS = ("NN", "NP", "NR", "VV", "VA", "XR", "SN", "SL")


class KiwiMorph:
    """kiwipiepy 형태소 분석 — 조사·어미를 떼고 내용어 원형만 남긴다.

    '보험금을 청구하려면' → ['보험금', '청구'] 처럼 문서·질의의 표면형이 달라도
    같은 토큰으로 정규화돼 sparse(BM25) recall이 오른다. 숫자·영문은 유지.
    """

    def __init__(self):
        from kiwipiepy import Kiwi  # import 실패는 build_morph가 처리
        self._kiwi = Kiwi()

    def tokens(self, text: str) -> list[str]:
        out: list[str] = []
        for tok in self._kiwi.tokenize(text):
            if tok.tag.startswith(_KIWI_TAGS):
                out.append(tok.form.lower())
        # 분석 결과가 비면(기호뿐 등) 어절 폴백 — 빈 sparse로 절름발이 검색 방지
        return out or _TOKEN.findall(text.lower())


def build_morph():
    """kiwipiepy가 있으면 형태소 분석, 없으면 어절 토크나이저 폴백.

    적재·질의가 반드시 같은 토크나이저를 쓰도록 이 팩토리 하나로 통일한다."""
    try:
        return KiwiMorph()
    except ImportError:
        return SimpleMorph()


class LocalHashEmbeddingModel:
    """결정적 해시 임베딩(bag-of-words + 문자 bigram). 키 불필요.

    같은 토큰은 항상 같은 버킷 → 프로세스 간 일관(운영 sparse 해시 버그와 무관하게
    dense 폴백은 결정적). 어휘 겹침 기반 코사인이라 키워드 질의 데모가 실제로 동작한다.
    """

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_id = f"local-hash-{dim}"

    def encode(self, texts: list[str], *, task: str | None = None) -> list[list[float]]:
        del task  # API 호환(로컬 해시는 task 무시)
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


def _approx_tokens(text: str) -> int:
    """청커와 같은 추정(한글 약관 기준 len//2)."""
    return max(1, len(text) // 2)


def _iter_token_batches(texts: list[str], batch_size: int, max_tokens: int):
    """개수·추정 토큰 상한으로 배치를 자른다. 한 청크가 상한을 넘으면 단독 전송."""
    batch: list[str] = []
    used = 0
    for text in texts:
        tok = _approx_tokens(text)
        if batch and (len(batch) >= batch_size or used + tok > max_tokens):
            yield batch
            batch = []
            used = 0
        batch.append(text)
        used += tok
    if batch:
        yield batch


def _tpm_pause(tokens: int, tpm_budget: int) -> float:
    if tpm_budget <= 0 or tokens <= 0:
        return 0.0
    return tokens / tpm_budget * 60.0


class ApiEmbeddingModel:
    """OpenAI 호환 임베딩 API 어댑터."""

    def __init__(self, api_base: str, api_key: str, model: str, dim: int,
                 timeout: float = 30.0, batch_size: int = 4,
                 send_dimensions: bool = False, max_retries: int = 8,
                 tpm_budget: int = 24000, max_tokens_per_request: int = 8000,
                 sleep=time.sleep, default_task: str | None = None):
        self._base = api_base.rstrip("/")
        self._key = api_key
        self.model_id = model
        self.dim = dim
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        # OpenAI text-embedding-3-*는 dimensions로 차원 축소 지원(저장량 절감).
        # 호환 프로바이더가 미지원일 수 있어 옵트인.
        self._send_dimensions = send_dimensions
        self._max_retries = max(0, max_retries)
        self._tpm_budget = max(1, tpm_budget)
        self._max_tokens_per_request = max(1, max_tokens_per_request)
        self._sleep = sleep
        self._default_task = (default_task or "").strip() or None
        # 커넥션 재사용(keep-alive) — 문서마다 새 TCP/TLS 핸드셰이크 방지.
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=timeout,
        )

    def encode(self, texts: list[str], *, task: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        effective = (task if task is not None else self._default_task)
        if isinstance(effective, str):
            effective = effective.strip() or None
        # 약관 PDF는 청크가 많아 Gemini 무료 TPM(3만/분)을 한 분에 넘긴다.
        # 배치를 토큰 상한으로 자르고, 전송 후 예산 비율만큼 쉰다.
        out: list[list[float]] = []
        for batch in _iter_token_batches(
                texts, self._batch_size, self._max_tokens_per_request):
            out.extend(self._encode_batch(batch, task=effective))
            pause = _tpm_pause(
                sum(_approx_tokens(t) for t in batch), self._tpm_budget)
            if pause:
                self._sleep(pause)
        return out

    def _encode_batch(self, batch: list[str], *, task: str | None = None) -> list[list[float]]:
        body: dict = {"model": self.model_id, "input": batch}
        if self._send_dimensions:
            body["dimensions"] = self.dim
        if task:
            body["task"] = task
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            resp = self._client.post("/embeddings", json=body)
            if resp.status_code == 200:
                return self._parse_vectors(resp, len(batch))
            if resp.status_code == 403:
                detail = ""
                try:
                    detail = str(resp.json())
                except Exception:  # noqa: BLE001
                    detail = resp.text[:200]
                raise EmbeddingRateLimitError(
                    f"403 insufficient balance: {detail}")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = httpx.HTTPStatusError(
                    f"{resp.status_code} embedding",
                    request=resp.request, response=resp)
                wait = _retry_wait(resp, attempt)
                logger.warning(
                    "embedding %s, retry %d/%d in %.1fs",
                    resp.status_code, attempt + 1, self._max_retries, wait)
                if attempt < self._max_retries:
                    self._sleep(wait)
                    continue
                raise EmbeddingRateLimitError(str(resp.status_code)) from last_err
            resp.raise_for_status()
        raise EmbeddingRateLimitError("exhausted") from last_err

    def _parse_vectors(self, resp: httpx.Response, n: int) -> list[list[float]]:
        data = resp.json()["data"]
        # index 순서 보장. 단 일부 호환 프로바이더(Gemini 등)는 index 필드를
        # 생략한다 — 그 경우 응답이 입력 순서라고 보고 정렬을 건너뛴다.
        if all(d.get("index") is not None for d in data):
            data.sort(key=lambda d: d["index"])
        vecs = [d["embedding"] for d in data]
        if len(vecs) != n:
            raise ValueError(f"embedding count mismatch: got {len(vecs)}, expected {n}")
        for v in vecs:
            if len(v) != self.dim:
                raise ValueError(
                    f"embedding dim mismatch: got {len(v)}, expected {self.dim} "
                    f"(EMBEDDING_DIM 설정 또는 컬렉션 재생성 필요)")
        return vecs


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    """429는 TPM 1분 창을 넘기도록 길게, 5xx는 짧은 지수 백오프."""
    if resp.status_code >= 500:
        return min(1.5 * (2 ** attempt), 16.0)
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if raw:
        try:
            return min(float(raw), 70.0)
        except ValueError:
            pass
    return max(65.0, min(1.5 * (2 ** attempt), 70.0))


def build_embedding_model(settings: Settings, *, default_task: str | None = None):
    """설정에 따라 임베딩 모델 생성(운영=API, 폴백=로컬 해시).

    default_task는 호출부가 명시할 때만 쓴다. 적재/질의 task는
    HybridEmbedder·QdrantVectorStore가 encode(..., task=)로 넘긴다.
    """
    if settings.embedding_provider == "openai" and settings.embedding_api_key:
        return ApiEmbeddingModel(
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            send_dimensions=settings.embedding_send_dimensions,
            batch_size=settings.embedding_batch_size,
            max_retries=settings.embedding_max_retries,
            tpm_budget=settings.embedding_tpm_budget,
            default_task=default_task,
        )
    return LocalHashEmbeddingModel(dim=settings.embedding_dim)
