"""보험사 공식 도메인 allowlist 안에서만 검색·페이지 조회."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from harag.contracts.boundaries import Chunk, ScoredChunk
from harag.schemas.chunk import ChunkMetadata, ChunkType

logger = logging.getLogger("harag.official_web")

_DOMAINS_PATH = Path(__file__).resolve().parent.parent / "guidance" / "insurer_domains.yaml"
WEB_DOCUMENT_ID = "web"
_WEB_SCORE = 0.95
_DDG = "https://html.duckduckgo.com/html/"

_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HREF_RE = re.compile(
    r'href=["\']([^"\']+)["\']',
    re.I,
)


@dataclass(frozen=True)
class AllowlistEntry:
    insurer: str
    hosts: frozenset[str]
    homepage: str
    source_url: str = ""
    display_name: str = ""


@dataclass
class WebBrowseConfig:
    enabled: bool = True
    max_fetches: int = 3
    max_chars: int = 6000
    timeout_sec: float = 8.0
    max_searches: int = 2
    max_plan_urls: int = 3


class WebPlanLLM(Protocol):
    def plan(self, *, query: str, hosts: list[str],
             product_name: str, insurer: str) -> dict: ...


@lru_cache(maxsize=1)
def load_insurer_domains() -> dict:
    if not _DOMAINS_PATH.is_file():
        return {}
    try:
        raw = json.loads(_DOMAINS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def is_host_allowed(url: str, allowed: set[str] | frozenset[str]) -> bool:
    h = host_of(url)
    if not h:
        return False
    allowed_l = {a.lower() for a in allowed}
    if h in allowed_l:
        return True
    # www. 변형
    bare = h[4:] if h.startswith("www.") else h
    return bare in allowed_l or f"www.{bare}" in allowed_l


def resolve_allowlist_for_products(
    products: list[dict],
) -> list[AllowlistEntry]:
    """products: {insurer, source_url, display_name}."""
    domains = load_insurer_domains()
    out: list[AllowlistEntry] = []
    seen: set[str] = set()
    for p in products or []:
        insurer = str(p.get("insurer") or "").strip()
        source_url = str(p.get("source_url") or "").strip()
        display = str(p.get("display_name") or "").strip()
        hosts: set[str] = set()
        homepage = ""
        rec = domains.get(insurer) if insurer else None
        if isinstance(rec, dict):
            for h in rec.get("hosts") or []:
                if h:
                    hosts.add(str(h).lower())
            homepage = str(rec.get("homepage") or "").strip()
        if source_url:
            h = host_of(source_url)
            if h:
                hosts.add(h)
            if not homepage:
                homepage = source_url
        if not hosts:
            continue
        key = insurer or sorted(hosts)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(AllowlistEntry(
            insurer=insurer or key,
            hosts=frozenset(hosts),
            homepage=homepage or f"https://{next(iter(hosts))}",
            source_url=source_url,
            display_name=display,
        ))
    return out


def html_to_text(html: str, max_chars: int) -> str:
    body = _SCRIPT_RE.sub(" ", html or "")
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()
    return body[:max_chars]


def _unwrap_ddg_redirect(url: str) -> str:
    """DuckDuckGo //duckduckgo.com/l/?uddg=... → 실제 URL."""
    try:
        p = urlparse(url)
        if "duckduckgo.com" in (p.netloc or "") and "/l/" in (p.path or ""):
            qs = parse_qs(p.query)
            uddg = (qs.get("uddg") or [None])[0]
            if uddg:
                return unquote(uddg)
    except Exception:  # noqa: BLE001
        pass
    return url


def scored_web_chunk(url: str, text: str, *, score: float = _WEB_SCORE) -> ScoredChunk:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    cid = f"web-{digest}"
    body = (
        "공식 사이트 발췌\n"
        f"URL: {url}\n"
        "아래는 보험사 공식 페이지에서 가져온 텍스트다. "
        "가입·공시·보험료 확인 경로 안내에 활용하고, "
        "담보·면책 확정 수치는 상품요약서 청크를 우선하라.\n\n"
        + (text or "")[:8000]
    )
    meta = ChunkMetadata(
        chunk_id=cid,
        document_id=WEB_DOCUMENT_ID,
        source_block_ids=[cid],
        struct_path="공식사이트",
        chunk_type=ChunkType.body,
        security_level="internal",
        acl_tags=["scope:library", "lib:knowledge"],
        source_document=url,
        citation_label=url,
        parsing_version="web-1",
        chunking_version="web-1",
        embedding_model_id="none",
        content_hash=digest,
        created_at=datetime.now(timezone.utc),
    )
    return ScoredChunk(chunk=Chunk(meta=meta, text=body), score=score)


def prepend_web_chunks(
    results: list[ScoredChunk], web: list[ScoredChunk],
) -> list[ScoredChunk]:
    if not web:
        return results
    seen = {r.chunk.meta.chunk_id for r in results}
    extra = [w for w in web if w.chunk.meta.chunk_id not in seen]
    return extra + list(results)


class HeuristicWebPlanner:
    """LLM 없을 때 가입·보험료 검색어 + homepage/source_url."""

    def plan(self, *, query: str, hosts: list[str],
             product_name: str, insurer: str) -> dict:
        q = (query or "").strip()
        searches: list[str] = []
        if product_name:
            searches.append(f"{product_name} 가입")
            searches.append(f"{product_name} 보험료")
        else:
            searches.append("가입 안내")
            searches.append("보험료 공시")
        if "보험료" in q or "얼마" in q:
            searches = [f"{product_name or insurer} 보험료 공시",
                        f"{product_name or insurer} 가격공시"]
        return {"searches": searches[:2], "urls": []}


class TransportWebPlanner:
    def __init__(self, transport, model: str):
        self._transport = transport
        self._model = model

    def plan(self, *, query: str, hosts: list[str],
             product_name: str, insurer: str) -> dict:
        prompt = (
            "보험사 공식 사이트만 조회할 검색 계획을 JSON으로 작성하라.\n"
            f"허용 호스트: {', '.join(hosts)}\n"
            f"보험사: {insurer}\n"
            f"상품: {product_name}\n"
            f"질의: {query[:400]}\n"
            "규칙: searches는 최대 2개 한국어 검색어, "
            "urls는 허용 호스트의 https URL만 최대 3개. "
            "다른 도메인·금액 확정·병명 생성 금지.\n"
            '출력 예: {"searches":["..."],"urls":["https://..."]}'
        )
        out = self._transport.post({
            "model": self._model,
            "prompt": prompt,
            "context_ids": [],
        })
        return _parse_plan(out.get("answer") or "")


def _parse_plan(raw: str) -> dict:
    text = (raw or "").strip()
    searches: list[str] = []
    urls: list[str] = []
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            obj = json.loads(text[start:end])
            searches = [
                str(s).strip() for s in (obj.get("searches") or [])
                if str(s).strip()
            ]
            urls = [
                str(u).strip() for u in (obj.get("urls") or [])
                if str(u).strip().startswith("http")
            ]
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.info("web plan JSON parse failed")
    return {"searches": searches, "urls": urls}


@dataclass
class OfficialWebBrowser:
    config: WebBrowseConfig = field(default_factory=WebBrowseConfig)
    planner: WebPlanLLM | None = None
    client: httpx.Client | None = None

    def browse(
        self,
        query: str,
        entries: list[AllowlistEntry],
    ) -> list[ScoredChunk]:
        if not self.config.enabled or not entries:
            return []
        planner = self.planner or HeuristicWebPlanner()
        owns_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.config.timeout_sec,
            follow_redirects=True,
            headers={"User-Agent": "harag-official-web/1.0"},
        )
        chunks: list[ScoredChunk] = []
        fetched: set[str] = set()
        try:
            for entry in entries[:2]:
                allowed = set(entry.hosts)
                plan = planner.plan(
                    query=query,
                    hosts=sorted(allowed),
                    product_name=entry.display_name,
                    insurer=entry.insurer,
                )
                urls: list[str] = []
                # seed: source_url / homepage
                for seed in (entry.source_url, entry.homepage):
                    if seed and is_host_allowed(seed, allowed):
                        urls.append(seed)
                for u in (plan.get("urls") or [])[: self.config.max_plan_urls]:
                    if is_host_allowed(u, allowed):
                        urls.append(u)
                    else:
                        logger.info("drop non-allowlisted plan url host=%s",
                                    host_of(u))
                for term in (plan.get("searches") or [])[: self.config.max_searches]:
                    host = next(iter(sorted(allowed)))
                    urls.extend(self._ddg_site_search(client, host, term, allowed))
                for url in urls:
                    if len(fetched) >= self.config.max_fetches:
                        break
                    if url in fetched:
                        continue
                    text = self._fetch_text(client, url, allowed)
                    if not text:
                        continue
                    final_url = url
                    fetched.add(final_url)
                    chunks.append(scored_web_chunk(final_url, text))
                if len(fetched) >= self.config.max_fetches:
                    break
        finally:
            if owns_client:
                client.close()
        return chunks

    def _ddg_site_search(
        self,
        client: httpx.Client,
        host: str,
        term: str,
        allowed: set[str],
    ) -> list[str]:
        q = f"site:{host} {term}".strip()
        try:
            resp = client.post(_DDG, data={"q": q})
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            logger.info("ddg search failed q=%s", q[:80], exc_info=True)
            return []
        found: list[str] = []
        for href in _HREF_RE.findall(resp.text or ""):
            href = _unwrap_ddg_redirect(href.strip())
            if not href.startswith("http"):
                continue
            if not is_host_allowed(href, allowed):
                continue
            if href not in found:
                found.append(href)
            if len(found) >= 2:
                break
        return found

    def _fetch_text(
        self,
        client: httpx.Client,
        url: str,
        allowed: set[str],
    ) -> str:
        if not is_host_allowed(url, allowed):
            return ""
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            logger.info("web fetch failed url=%s", url[:120], exc_info=True)
            return ""
        # 리다이렉트 후 host 재검증
        final = str(resp.url)
        if not is_host_allowed(final, allowed):
            logger.info("drop redirect outside allowlist %s → %s",
                        url[:80], final[:80])
            return ""
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype and ctype:
            return ""
        raw = resp.content[: self.config.max_chars * 4]
        try:
            html = raw.decode(resp.encoding or "utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            html = raw.decode("utf-8", errors="ignore")
        return html_to_text(html, self.config.max_chars)
