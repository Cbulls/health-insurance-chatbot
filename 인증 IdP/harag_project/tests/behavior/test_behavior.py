"""
TDD 빨강 테스트 골격 — 파서·검색의 동작 테스트.

이 테스트들은 '지금 실패한다'. 구현(Parser, Retriever)이 없기 때문이다.
TDD 사이클: 이 빨강을 초록으로 만드는 최소 구현 → 리팩터.

실행: pytest test_behavior_red.py -v
지금 실행하면 전부 실패(혹은 skip)한다. 그것이 TDD의 출발점이다.

기존 계약(ir_schema, component_contracts 등)을 재사용해 '나와야 할 출력의 형태'를 고정한다.
"""
import pytest

from harag.schemas.ir import DocumentIR, BlockType, ParseStatus
from harag.contracts.boundaries import Chunk, ScoredChunk, AuthContext


# ════════════════════════════════════════════════════════
# 아직 존재하지 않는 구현들 (TDD가 이끌 대상)
# ════════════════════════════════════════════════════════
def get_parser():
    """구현되면 Parser 인스턴스를 반환. 지금은 없다."""
    try:
        from harag.parsing.parser import HwpParser  # 아직 없음
        return HwpParser()
    except ImportError:
        return None

def get_retriever():
    try:
        from harag.retrieval.hybrid import HybridRetriever  # 아직 없음
        return HybridRetriever()
    except ImportError:
        return None


# 테스트 픽스처 경로(골든 표본 — 국면 A PoC 표본과 공유)
GOLDEN_HWP_WITH_TABLE = "fixtures/golden/regulation_with_merged_table.hwp"
GOLDEN_HWPX = "fixtures/golden/sample.hwpx"


# ════════════════════════════════════════════════════════
# 파서 테스트는 tests/behavior/test_parser.py로 이전(StructuringParser)
# ════════════════════════════════════════════════════════
class TestRetrieval:

    def setup_method(self):
        self.retriever = get_retriever()
        if self.retriever is not None:
            # 시드: finance 전용 청크와 hr 전용 청크를 적재(테스트 자립)
            self.retriever.index([
                _seed_chunk("fin1", ["dept:finance"], "국내출장 여비 한도는 1일 5만원 제24조의2"),
                _seed_chunk("hr1", ["dept:hr"], "연차휴가는 15일 부여"),
            ])

    def _require_impl(self):
        if self.retriever is None:
            pytest.fail("RED: Retriever 미구현 — TDD 목표")

    def test_R02_clause_number_exact_match(self):
        """R-02: 조항번호 질의는 정확 매칭(sparse 효과) — dense 단독은 실패할 케이스"""
        self._require_impl()
        auth = AuthContext(user_id="u1", acl_tags=frozenset({"dept:finance"}))
        results = self.retriever.retrieve("제24조의2", auth=auth, k=5)
        # 정확히 '제24조의2'를 포함한 청크가 상위에 와야
        assert any("제24조의2" in r.chunk.text for r in results[:3]), \
            "조항번호 정확 매칭 실패(dense-only 의심)"

    def test_R04_hybrid_beats_single(self):
        """R-04: 하이브리드 recall > dense 단독, sparse 단독"""
        self._require_impl()
        auth = AuthContext(user_id="u1", acl_tags=frozenset({"dept:finance"}))
        # 구현은 모드별 검색을 노출해야 함(테스트가 설계를 요구)
        q = "출장비 정산 기한"
        hybrid = {r.chunk.meta.chunk_id for r in self.retriever.retrieve(q, auth, k=10)}
        dense = {r.chunk.meta.chunk_id for r in self.retriever.retrieve(q, auth, k=10, mode="dense")}
        sparse = {r.chunk.meta.chunk_id for r in self.retriever.retrieve(q, auth, k=10, mode="sparse")}
        gold = {"gold_chunk_1"}  # 골드셋에서
        # 하이브리드가 둘 중 어느 것보다 정답 포함이 같거나 나아야
        assert (gold & hybrid) >= (gold & dense) and (gold & hybrid) >= (gold & sparse)

    def test_R05_acl_excludes_unauthorized(self):
        """R-05: 권한 없는 사용자는 권한 외 청크 회수 불가.
        주의: '권한 외가 안 나온다'만 보면 빈 인덱스도 통과(vacuous). 그래서 양방향 검증."""
        self._require_impl()
        fin_user = AuthContext(user_id="u1", acl_tags=frozenset({"dept:finance"}))
        hr_user = AuthContext(user_id="u2", acl_tags=frozenset({"dept:hr"}))

        # ① 권한 있는 사용자는 finance 문서를 회수한다(positive — vacuous 방지)
        fin_results = self.retriever.retrieve("출장비 한도", auth=fin_user, k=10)
        assert any("dept:finance" in r.chunk.meta.acl_tags for r in fin_results), \
            "권한 있는 사용자가 자기 문서를 못 받음(retriever가 비었거나 깨짐)"

        # ② 권한 없는 사용자에겐 finance 전용 청크가 안 보인다(negative)
        hr_results = self.retriever.retrieve("출장비 한도", auth=hr_user, k=10)
        leaked = [r for r in hr_results
                  if "dept:finance" in r.chunk.meta.acl_tags
                  and "dept:hr" not in r.chunk.meta.acl_tags]
        assert not leaked, f"권한 외 청크 누수: {[r.chunk.meta.chunk_id for r in leaked]}"

    def test_R06_acl_applied_to_both_dense_and_sparse(self):
        """R-06: ACL이 dense·sparse 양쪽에 동일 적용(누수 A).
        한쪽 인덱스에만 필터 걸리면, 그 경로로 권한 외 청크가 후보에 진입한다.
        질의를 finance 청크 본문과 강하게 매칭시켜, 필터가 없으면 반드시 회수되게 한다."""
        self._require_impl()
        hr_user = AuthContext(user_id="u2", acl_tags=frozenset({"dept:hr"}))
        # finance 청크 본문의 단어를 그대로 사용 → 필터 없으면 확실히 매칭됨
        leaky_query = "국내출장 여비 한도 제24조의2"
        for mode in ("dense", "sparse"):
            res = self.retriever.retrieve(leaky_query, auth=hr_user, k=10, mode=mode)
            leaked = [r for r in res if "dept:finance" in r.chunk.meta.acl_tags
                      and "dept:hr" not in r.chunk.meta.acl_tags]
            assert not leaked, f"{mode} 경로로 권한 외 청크 누수(누수 A)"


# ════════════════════════════════════════════════════════
# 3. 생성 동작 테스트 (G-01, G-03) — abstention & 위조 인용
# ════════════════════════════════════════════════════════
class TestGeneration:

    def _get_generator(self):
        try:
            from harag.generation.generator import AnswerGenerator  # 아직 없음
            return AnswerGenerator()
        except ImportError:
            return None

    def test_G01_empty_context_abstains(self):
        """G-01: 빈 컨텍스트 → 지어내지 않고 abstain"""
        gen = self._get_generator()
        if gen is None:
            pytest.fail("RED: Generator 미구현 — TDD 목표")
        result = gen.generate(query="우주여행 출장비는?", context=[])
        assert result.answer is None, "빈 컨텍스트인데 답을 지어냄(환각)"

    def test_G03_citation_must_exist_in_context(self):
        """G-03: 답변 인용 ID가 실제 컨텍스트에 존재(위조 인용 차단)"""
        gen = self._get_generator()
        if gen is None:
            pytest.fail("RED: Generator 미구현 — TDD 목표")
        # 컨텍스트에 chunk 'real1'만 줬는데 답이 'fake'를 인용하면 차단
        ctx = [ScoredChunk(chunk=Chunk(meta=_make_meta("real1"), text="..."), score=0.8)]
        result = gen.generate(query="...", context=ctx)
        if result.answer is not None:
            assert all(c in {"real1"} for c in result.cited_chunk_ids), \
                "컨텍스트에 없는 청크를 인용함(위조)"

    def test_G02_low_score_context_abstains(self):
        """G-02: 컨텍스트는 있지만 전부 저점수 → abstain(억지 답변 금지)"""
        gen = self._get_generator()
        if gen is None:
            pytest.fail("RED: Generator 미구현 — TDD 목표")
        low = [ScoredChunk(chunk=Chunk(meta=_make_meta("c1"), text="..."), score=0.1)]
        result = gen.generate(query="...", context=low)
        assert result.answer is None and result.abstained, "저점수인데 억지로 답함"

    def test_G05_valid_context_produces_answer_with_real_citation(self):
        """G-05(축소): 정상 컨텍스트 → 답변 생성 + 인용은 실제 청크"""
        gen = self._get_generator()
        if gen is None:
            pytest.fail("RED: Generator 미구현 — TDD 목표")
        ctx = [ScoredChunk(chunk=Chunk(meta=_make_meta("real1"), text="출장비 한도는 5만원"), score=0.9)]
        result = gen.generate(query="출장비 한도?", context=ctx)
        assert result.answer is not None, "정상 컨텍스트인데 답을 안 함"
        assert all(c in {"real1"} for c in result.cited_chunk_ids)

    def test_G04_fabricated_citation_is_blocked_not_returned(self):
        """G-04: LLM이 위조 인용을 내면 답이 차단되고 abstain된다"""
        gen = self._get_generator()
        if gen is None:
            pytest.fail("RED: Generator 미구현 — TDD 목표")
        # 위조 인용을 강제하는 악성 LLM 주입
        class FabricatingLLM:
            def complete(self, query, context_texts, context_ids):
                return ("그럴듯한 답", ["ghost_id"])  # 컨텍스트에 없는 id
        from harag.generation.generator import AnswerGenerator
        g = AnswerGenerator(llm=FabricatingLLM())
        ctx = [ScoredChunk(chunk=Chunk(meta=_make_meta("real1"), text="..."), score=0.9)]
        result = g.generate(query="...", context=ctx)
        assert result.answer is None and "fabricated" in (result.abstain_reason or ""), \
            "위조 인용이 차단되지 않음"


def _make_meta(chunk_id):
    from datetime import datetime, timezone
    from harag.schemas.chunk import ChunkMetadata, ChunkType
    return ChunkMetadata(
        chunk_id=chunk_id, document_id="d1", source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="internal", acl_tags=["dept:fin"],
        source_document="x.hwp", citation_label="제1조", parsing_version="p1",
        chunking_version="c1", embedding_model_id="kure-v1", content_hash="h",
        created_at=datetime.now(timezone.utc))


def _seed_chunk(chunk_id, acl_tags, text):
    """검색 테스트용 시드 청크(메타+텍스트)."""
    from datetime import datetime, timezone
    from harag.schemas.chunk import ChunkMetadata, ChunkType
    meta = ChunkMetadata(
        chunk_id=chunk_id, document_id="d1", source_block_ids=["b1"],
        chunk_type=ChunkType.body, security_level="internal", acl_tags=acl_tags,
        source_document="x.hwp", citation_label="제1조", parsing_version="p1",
        chunking_version="c1", embedding_model_id="kure-v1", content_hash="h",
        created_at=datetime.now(timezone.utc))
    return Chunk(meta=meta, text=text)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
