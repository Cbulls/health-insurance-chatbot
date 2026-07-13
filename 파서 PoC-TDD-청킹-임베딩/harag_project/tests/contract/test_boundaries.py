"""B-2 경계 계약 테스트 — 각 경계가 self-critique 위협을 실제로 거부하는지 증명."""
from datetime import datetime, timezone

from harag.schemas.ir import (DocumentIR, Block, BlockType, TableContent, TableCell,
                       SourceFormat, ExtractionPath, ParseStatus, SourceMetadata, ParseQuality)
from harag.schemas.chunk import ChunkMetadata, ChunkType
from harag.contracts.boundaries import (
    Chunk, EmbeddedChunk, ScoredChunk, AuthContext, QualityGate,
    gate_boundary1, verify_boundary2, verify_boundary3,
    filter_by_auth, verify_boundary5_citations, must_abstain, ContractViolation,
)

NOW = datetime.now(timezone.utc)
PASS, FAIL = [], []

def expect_ok(name, fn):
    try: fn(); PASS.append(name)
    except Exception as e: FAIL.append(f"{name} — raised {type(e).__name__}: {e}")

def expect_reject(name, fn):
    try: fn(); FAIL.append(f"{name} — should REJECT but accepted")
    except (ContractViolation, ValueError): PASS.append(name)


def meta(**o):
    base = dict(chunk_id="c1", document_id="d1", source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="internal", acl_tags=["dept:fin"],
        source_document="x.hwp", citation_label="제12조", parsing_version="p1",
        chunking_version="ck1", embedding_model_id="kure-v1", content_hash="h",
        created_at=NOW)
    base.update(o); return ChunkMetadata(**base)

def para(bid, oi, noise=False):
    return Block(block_id=bid, block_type=BlockType.paragraph, order_index=oi,
                 confidence=0.9, text="본문", is_noise=noise)

def tbl(bid, oi):
    return Block(block_id=bid, block_type=BlockType.table, order_index=oi, confidence=0.9,
        table_content=TableContent(n_rows=1, n_cols=1,
            cells=[TableCell(row=0, col=0, text="x")], recovery_confidence=0.9))

def doc(blocks, **o):
    base = dict(document_id="d1", source_format=SourceFormat.hwp5,
        extraction_path=ExtractionPath.native, parser_version="p1", ingested_at=NOW,
        source_metadata=SourceMetadata(source_system="e", department="f",
            security_level="internal", original_path="/x"),
        parse_quality=ParseQuality(char_count=100, table_recovery_avg=0.95,
            broken_char_ratio=0.0, order_confidence=0.9),
        parse_status=ParseStatus.ok, blocks=blocks)
    base.update(o); return DocumentIR(**base)


# ════════ 경계1: PoC 임계가 런타임 게이트로 작동 ════════
GATE = QualityGate(min_table_recovery=0.85, max_broken_char_ratio=0.05, min_order_confidence=0.8)
expect_ok("B1/good doc passes gate",
    lambda: (gate_boundary1(doc([para("b1", 1)]), GATE) is True) or (_ for _ in ()).throw(AssertionError()))

def b1_low_table():
    d = doc([para("b1", 1)], parse_quality=ParseQuality(char_count=100,
        table_recovery_avg=0.60, broken_char_ratio=0.0, order_confidence=0.9))
    assert gate_boundary1(d, GATE) is False
expect_ok("B1/low table_recovery quarantined", b1_low_table)

def b1_failed():
    d = doc([para("b1", 1)], parse_status=ParseStatus.partial,
        parse_quality=ParseQuality(char_count=0, table_recovery_avg=0.0,
        broken_char_ratio=0.9, order_confidence=0.1))
    assert gate_boundary1(d, GATE) is False
expect_ok("B1/broken doc quarantined", b1_failed)


# ════════ 경계2: M3 교차검증이 닫힌다 ════════
d2 = doc([para("b1", 1), tbl("t1", 2)])

expect_ok("B2/valid body chunk passes",
    lambda: verify_boundary2(d2, [Chunk(meta=meta(source_block_ids=["b1"]), text="본문")]))

expect_ok("B2/valid table chunk passes",
    lambda: verify_boundary2(d2, [Chunk(
        meta=meta(chunk_id="ct", chunk_type=ChunkType.table, source_block_ids=["t1"]),
        text="표")]))

# M3 위반: table 청크가 paragraph 블록을 가리킴
expect_reject("B2/M3 table chunk tracing to non-table block rejected",
    lambda: verify_boundary2(d2, [Chunk(
        meta=meta(chunk_id="bad", chunk_type=ChunkType.table, source_block_ids=["b1"]),
        text="가짜표")]))

# 존재하지 않는 블록 인용(인용 체인 끊김)
expect_reject("B2/chunk citing non-existent block rejected",
    lambda: verify_boundary2(d2, [Chunk(
        meta=meta(source_block_ids=["ghost"]), text="x")]))

# 노이즈 블록(머리말)에서 온 청크 — 검색 오염 차단
d2n = doc([para("b1", 1), para("hf", 2, noise=True)])
expect_reject("B2/chunk from noise block rejected",
    lambda: verify_boundary2(d2n, [Chunk(
        meta=meta(source_block_ids=["hf"]), text="머리말")]))


# ════════ 경계3: 모델 교체 후 stale 벡터·반쪽 하이브리드 차단 ════════
def emb(dim=1024, model="kure-v1", sparse=True):
    return EmbeddedChunk(chunk=Chunk(meta=meta(), text="x"),
        dense_vector=[0.1]*dim, sparse_terms={"출장": 1.2} if sparse else {})

expect_ok("B3/correct embedding passes",
    lambda: verify_boundary3([emb()], expected_dim=1024, expected_model_id="kure-v1"))
expect_reject("B3/stale dimension rejected",
    lambda: verify_boundary3([emb(dim=768)], expected_dim=1024, expected_model_id="kure-v1"))
expect_reject("B3/missing sparse (half-broken hybrid) rejected",
    lambda: verify_boundary3([emb(sparse=False)], expected_dim=1024, expected_model_id="kure-v1"))


# ════════ 경계4: ACL 누수 차단 ════════
auth = AuthContext(user_id="u1", acl_tags=frozenset({"dept:fin"}))
sc_allowed = ScoredChunk(chunk=Chunk(meta=meta(acl_tags=["dept:fin"]), text="x"), score=0.9)
sc_denied = ScoredChunk(chunk=Chunk(meta=meta(chunk_id="c2", acl_tags=["dept:hr"]), text="y"), score=0.95)

def b4_filters():
    out = filter_by_auth([sc_allowed, sc_denied], auth)
    assert len(out) == 1 and out[0].chunk.meta.chunk_id == "c1", "denied chunk leaked!"
expect_ok("B4/unauthorized chunk filtered out", b4_filters)


# ════════ 경계5: 위조 인용 + abstention ════════
ctx = [ScoredChunk(chunk=Chunk(meta=meta(chunk_id="real1"), text="x"), score=0.8)]
expect_ok("B5/valid citation passes",
    lambda: verify_boundary5_citations(["real1"], ctx))
expect_reject("B5/fabricated citation rejected",
    lambda: verify_boundary5_citations(["real1", "made_up"], ctx))

def b5_abstain_empty():
    assert must_abstain([], min_score=0.5) is True
expect_ok("B5/empty context -> abstain", b5_abstain_empty)
def b5_abstain_lowscore():
    low = [ScoredChunk(chunk=Chunk(meta=meta(), text="x"), score=0.2)]
    assert must_abstain(low, min_score=0.5) is True
expect_ok("B5/all-low-score -> abstain", b5_abstain_lowscore)
def b5_generate():
    ok = [ScoredChunk(chunk=Chunk(meta=meta(), text="x"), score=0.7)]
    assert must_abstain(ok, min_score=0.5) is False
expect_ok("B5/good context -> generate", b5_generate)


print(f"\n{'='*60}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for f in FAIL: print("  X", f)
if not FAIL: print("  OK 모든 경계 계약이 위협을 거부함")
print('='*60)
