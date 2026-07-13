"""harag — 한글 행정문서 RAG 챗봇.

MVP: PDF 업로드 → 청킹 → 임베딩 → Qdrant 검색(dense) → 외부 LLM 생성 → 출처 포함 응답.
엔터프라이즈 기능(HWP·조직 ACL·하이브리드+RRF·버전전환·GPU)은 Phase 2.
"""

__version__ = "0.1.0-mvp"
