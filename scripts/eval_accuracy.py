"""
PDF 답변 정확도 평가 — 골드 QA셋 기반 전/후 비교용.

서버 없이 인프로세스로 전체 파이프라인(파싱 → 청킹 → 임베딩 → 검색 → 리랭크 →
생성)을 구성해 평가한다. Qdrant는 항상 :memory:(운영 컬렉션 오염 방지).

사용:
  python scripts/eval_accuracy.py --local -o eval_results/baseline.json
    --local : 임베딩·LLM을 로컬 폴백으로 강제(오프라인·결정적 — 전/후 비교 기준)
    (플래그 없으면 .env의 프로바이더 설정을 따른다 — 실 API 품질 측정)

골드 정답은 chunk_id가 아니라 '페이지'로 정의한다. 청킹 방식이 바뀌어도
같은 골드셋으로 전/후를 공정하게 비교하기 위함이다(chunk_id는 청킹에 종속).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


# ── 골드 QA셋(문서: scripts/make_eval_pdf.py의 샘플 약관) ──
@dataclass(frozen=True)
class GoldQA:
    qid: str
    query: str
    qtype: str            # body | table | multi_clause | absent
    gold_pages: frozenset  # 근거 페이지 집합(absent면 빈 집합)
    expect_keywords: tuple = ()  # 답변에 있어야 할 키워드(correctness 프록시)


GOLD_SET: list[GoldQA] = [
    GoldQA("q1", "How many days do I have to file a claim after treatment?",
           "body", frozenset({2}), ("30",)),
    GoldQA("q2", "What documents are required to file a claim?",
           "body", frozenset({2}), ("diagnosis",)),
    GoldQA("q3", "What is the lifetime maximum benefit of this policy?",
           "body", frozenset({3}), ("100 million",)),
    GoldQA("q4", "Is cosmetic surgery covered by this policy?",
           "body", frozenset({4}), ("cosmetic",)),
    GoldQA("q5", "What is the annual limit for dental treatment?",
           "table", frozenset({3}), ("1,000,000",)),
    GoldQA("q6", "What is the copayment rate for inpatient treatment?",
           "table", frozenset({3}), ("20",)),
    GoldQA("q7", "What is the annual limit for outpatient visits?",
           "table", frozenset({3}), ("200,000",)),
    GoldQA("q8", "Which expenses does the insurer cover, and what is the "
                 "copayment for outpatient care?",
           "multi_clause", frozenset({2, 3}), ("30",)),
    GoldQA("q9", "What is the recipe for kimchi stew?",
           "absent", frozenset(), ()),
    GoldQA("q10", "What is the monthly premium for a 40-year-old male?",
           "absent", frozenset(), ()),
]


def build_pipeline():
    """api.main._build_and_inject를 평가용으로 재현(메타 DB 제외, Qdrant 인메모리)."""
    from harag.config.settings import get_settings
    from harag.embedding.api_embedder import build_embedding_model, build_morph
    from harag.embedding.embedder import HybridEmbedder
    from harag.retrieval.qdrant_store import QdrantVectorStore
    from harag.parsing.pdf_parser import PdfParser
    from harag.chunking.chunker import StructuralChunker, ChunkingContext
    from harag.generation.generator import AnswerGenerator
    from harag.llm.factory import build_llm_client
    from harag.llm.http_transport import OpenAIChatTransport
    from harag.retrieval.reranker import CrossEncoderReranker
    from harag.llm.local_rerank import LexicalCrossEncoder, LLMCrossEncoder

    settings = get_settings()
    embedding_model = build_embedding_model(settings)
    morph = build_morph()
    embedder = HybridEmbedder(embedding_model, morph)
    store = QdrantVectorStore(
        embedding_model=embedding_model, dim=embedding_model.dim,
        collection="eval_accuracy", url=None, morph=morph,
    )
    llm = build_llm_client(settings)
    generator = AnswerGenerator(llm=llm, min_score=settings.min_score)
    # 리랭커 구성은 api.main과 동일(LLM 키 있으면 LLM 리랭커, 아니면 어휘)
    if (settings.rerank_llm_enabled and settings.llm_provider == "openai"
            and settings.llm_api_key):
        transport = OpenAIChatTransport(
            api_base=settings.llm_api_base, api_key=settings.llm_api_key)
        model = (settings.rerank_llm_model or settings.llm_rewrite_model
                 or settings.llm_model)
        cross_encoder, blend = LLMCrossEncoder(transport, model), 0.3
    else:
        cross_encoder, blend = LexicalCrossEncoder(), 0.7
    reranker = CrossEncoderReranker(
        model=cross_encoder, top_n=min(5, settings.top_k),
        min_score=0.0, retrieval_blend=blend,
    )
    return dict(settings=settings, parser=PdfParser(), embedder=embedder,
                store=store, generator=generator, reranker=reranker,
                chunker=StructuralChunker(), ctx_cls=ChunkingContext)


def ingest(components, pdf_path: str, owner: str = "eval") -> list:
    raw = Path(pdf_path).read_bytes()
    ir = components["parser"].parse(raw, document_id="eval-doc",
                                    filename=Path(pdf_path).name)
    assert ir.parse_status.value == "ok", f"parse failed: {ir.parse_status}"
    ctx = components["ctx_cls"](
        acl_tags=[f"owner:{owner}"],
        source_document=Path(pdf_path).name,
        embedding_model_id=components["embedder"].model_id,
    )
    chunks = components["chunker"].chunk(ir, ctx)
    embedded = components["embedder"].embed(chunks)
    components["store"].index(embedded)
    return chunks


def _pages_of(chunk) -> set:
    ref = chunk.meta.page_ref
    if ref is None:
        return set()
    return set(ref) if isinstance(ref, list) else {ref}


def _generate_with_retry(generator, query, context, retries: int = 3):
    """일시 장애(503 등)는 재시도, 소진 시 abstain으로 강등(파이프라인과 동일)."""
    import time
    from harag.generation.generator import GenerationResult
    from harag.generation.llm_client import LLMError

    for attempt in range(retries + 1):
        try:
            return generator.generate(query=query, context=context)
        except LLMError as e:
            if attempt >= retries:
                print(f"  ! LLM error after {retries} retries: {e}")
                return GenerationResult(answer=None, abstained=True,
                                        abstain_reason="llm_unavailable")
            # 분당 쿼터(RPM) 소진은 짧은 백오프로 안 풀린다 — 창이 넘어가게 대기
            time.sleep(20.0 * (attempt + 1))


def run_eval(components, k: int = 5) -> dict:
    from harag.contracts.boundaries import AuthContext

    auth = AuthContext(user_id="eval", acl_tags=frozenset({"owner:eval"}))
    settings = components["settings"]
    store, reranker, generator = (
        components["store"], components["reranker"], components["generator"])

    # API 모드는 질의당 LLM 2회(리랭크+생성) — 무료 등급 RPM 아래로 페이싱
    pace_sec = 0.0 if settings.llm_provider == "local" else 8.0

    per_query = []
    for qi, g in enumerate(GOLD_SET):
        if qi and pace_sec:
            import time
            time.sleep(pace_sec)
        retrieved = store.retrieve(g.query, auth=auth, k=settings.top_k)
        final = reranker.rerank(g.query, retrieved)
        gen = _generate_with_retry(generator, g.query, final)

        ranked_pages = [_pages_of(sc.chunk) for sc in final]
        # hit@k / MRR — 페이지 기준(absent는 중립 1.0)
        if g.gold_pages:
            hit = any(p & g.gold_pages for p in ranked_pages[:k])
            rr = 0.0
            for rank, pages in enumerate(ranked_pages, start=1):
                if pages & g.gold_pages:
                    rr = 1.0 / rank
                    break
            # multi_clause: 모든 근거 페이지가 top-k 안에 있는가
            covered = set().union(*ranked_pages[:k]) if ranked_pages else set()
            recall = len(covered & g.gold_pages) / len(g.gold_pages)
        else:
            hit, rr, recall = True, 1.0, 1.0

        abstained = gen.answer is None
        abstention_ok = abstained if g.qtype == "absent" else not abstained

        answer_text = gen.answer or ""
        if g.expect_keywords and answer_text:
            correct = all(kw.lower() in answer_text.lower()
                          for kw in g.expect_keywords)
        elif g.qtype == "absent":
            correct = abstained
        else:
            correct = None if abstained else True

        # 인용 정합: 인용된 청크가 실제 컨텍스트에 존재 + (있다면) 골드 페이지 적중
        cited = gen.cited_chunk_ids
        ctx_ids = {sc.chunk.meta.chunk_id for sc in final}
        cite_exists = all(c in ctx_ids for c in cited) if cited else True
        if cited and g.gold_pages:
            by_id = {sc.chunk.meta.chunk_id: sc.chunk for sc in final}
            cite_on_gold = any(
                _pages_of(by_id[c]) & g.gold_pages for c in cited if c in by_id)
        else:
            cite_on_gold = None

        per_query.append(dict(
            qid=g.qid, qtype=g.qtype, query=g.query,
            hit_at_k=hit, mrr=round(rr, 4), page_recall=round(recall, 4),
            abstained=abstained, abstention_ok=abstention_ok,
            correct=correct, cite_exists=cite_exists, cite_on_gold=cite_on_gold,
            n_cited=len(cited),
            answer=(answer_text[:200] if answer_text else None),
            top_pages=[sorted(p) for p in ranked_pages[:k]],
        ))

    # 유형별 집계(단일 평균 금지 — eval/harness.py 원칙)
    by_type: dict[str, list[dict]] = {}
    for r in per_query:
        by_type.setdefault(r["qtype"], []).append(r)
    summary = {}
    for qtype, rs in sorted(by_type.items()):
        n = len(rs)
        graded = [r for r in rs if r["correct"] is not None]
        summary[qtype] = dict(
            n=n,
            hit_at_k=round(sum(r["hit_at_k"] for r in rs) / n, 3),
            mrr=round(sum(r["mrr"] for r in rs) / n, 3),
            page_recall=round(sum(r["page_recall"] for r in rs) / n, 3),
            abstention_acc=round(sum(r["abstention_ok"] for r in rs) / n, 3),
            correct=round(sum(bool(r["correct"]) for r in graded)
                          / len(graded), 3) if graded else None,
        )
    return dict(summary=summary, per_query=per_query)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true",
                    help="임베딩·LLM을 로컬 폴백으로 강제(오프라인·결정적)")
    ap.add_argument("-o", "--output", default="",
                    help="결과 JSON 저장 경로(예: eval_results/baseline.json)")
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    # 운영 오염 방지: Qdrant는 build_pipeline에서 url=None(인메모리) 고정.
    if args.local:
        os.environ["EMBEDDING_PROVIDER"] = "local"
        os.environ["LLM_PROVIDER"] = "local"
    os.environ["LLM_REWRITE_ENABLED"] = "false"  # 평가는 단일턴 — 재작성 불필요

    from make_eval_pdf import make_pdf, POLICY_PAGES

    pdf_path = str(Path(__file__).parent / "sample_health_policy.pdf")
    make_pdf(pdf_path, POLICY_PAGES)

    components = build_pipeline()
    chunks = ingest(components, pdf_path)
    print(f"ingested: {len(chunks)} chunks "
          f"(embedding={components['embedder'].model_id})")

    report = run_eval(components, k=args.k)
    report["meta"] = dict(
        ran_at=datetime.now(timezone.utc).isoformat(),
        k=args.k,
        local=args.local,
        embedding_model=components["embedder"].model_id,
        llm_provider=components["settings"].llm_provider,
        n_chunks=len(chunks),
    )

    print(f"\n{'type':<14}{'n':>3}{'hit@k':>8}{'mrr':>8}{'recall':>8}"
          f"{'abst.':>8}{'correct':>9}")
    for qtype, s in report["summary"].items():
        corr = "-" if s["correct"] is None else f"{s['correct']:.3f}"
        print(f"{qtype:<14}{s['n']:>3}{s['hit_at_k']:>8.3f}{s['mrr']:>8.3f}"
              f"{s['page_recall']:>8.3f}{s['abstention_acc']:>8.3f}{corr:>9}")

    fails = [r for r in report["per_query"]
             if not (r["hit_at_k"] and r["abstention_ok"]
                     and r["correct"] in (True, None))]
    if fails:
        print("\nfailures:")
        for r in fails:
            print(f"  {r['qid']} [{r['qtype']}] hit={r['hit_at_k']} "
                  f"abst_ok={r['abstention_ok']} correct={r['correct']} "
                  f"top_pages={r['top_pages']}")
            if r["answer"]:
                print(f"    answer: {r['answer'][:120]}")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
