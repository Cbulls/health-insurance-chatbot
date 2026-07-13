"""
파싱 출력 중간표현(IR) 스키마 — 코드 계약.

설계 원칙:
  - IR은 평문도 최종 마크다운도 아닌 '블록 트리 + 블록별 메타'다.
  - 표는 절대 문자열로 평탄화하지 않는다(셀 좌표 보존). 마크다운은 이 트리에서 렌더하는 파생물.
  - 불변식은 주석이 아니라 validator로 강제한다. 위반 객체는 생성 자체가 불가능하다.

핵심 불변식(국면 B에서 동결):
  I1. block_id는 문서 내 유일.
  I2. order_index는 전 블록에 걸쳐 전순서(중복 금지) — 읽기 순서 복원 실패를 숨기지 않음.
  I3. table 블록은 반드시 셀 구조를 가진다. 문자열 표는 스키마 위반.
  I4. struct_path는 비어도 되지만 거짓이면 안 됨(모르면 빈 값; 추측 금지는 파서 책임).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field, model_validator, ConfigDict

SCHEMA_VERSION = "ir-1.0.0"


# ── 열거형 ──────────────────────────────────────────────
class SourceFormat(str, Enum):
    hwp5 = "hwp5"          # 구형 바이너리
    hwpx = "hwpx"
    pdf_text = "pdf_text"
    pdf_scan = "pdf_scan"
    docx = "docx"
    pptx = "pptx"
    xlsx = "xlsx"
    html = "html"
    md = "md"
    other = "other"


class ExtractionPath(str, Enum):
    """FR-2 라우팅 결과. 다운스트림 신뢰도·라우팅 튜닝의 근거."""
    native = "native"
    pdf_via = "pdf_via"
    multimodal = "multimodal"


class ParseStatus(str, Enum):
    ok = "ok"
    partial = "partial"
    failed = "failed"


class BlockType(str, Enum):
    heading = "heading"
    paragraph = "paragraph"
    table = "table"
    list = "list"
    footnote = "footnote"
    caption = "caption"
    header_footer = "header_footer"  # is_noise=true 대상
    figure = "figure"


# ── 표 페이로드(가장 중요) ───────────────────────────────
class TableCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    text: str
    is_header: bool = False


class TableContent(BaseModel):
    """셀 좌표 보존. recovery_confidence가 낮으면 다운스트림이 '신뢰 말 것'을 안다."""
    model_config = ConfigDict(extra="forbid")
    n_rows: int = Field(ge=1)
    n_cols: int = Field(ge=1)
    header_rows: list[int] = Field(default_factory=list)
    cells: list[TableCell]
    recovery_confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _cells_within_bounds(self) -> "TableContent":
        if not self.cells:
            raise ValueError("table must have at least one cell (no empty/string tables)")
        for c in self.cells:
            if c.row + c.row_span > self.n_rows:
                raise ValueError(f"cell row {c.row}+span exceeds n_rows {self.n_rows}")
            if c.col + c.col_span > self.n_cols:
                raise ValueError(f"cell col {c.col}+span exceeds n_cols {self.n_cols}")
        for hr in self.header_rows:
            if not (0 <= hr < self.n_rows):
                raise ValueError(f"header_row {hr} out of range")
        return self


# ── 블록(트리 노드) ─────────────────────────────────────
class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_id: str
    block_type: BlockType
    struct_path: str = ""          # I4: 비어도 됨, 거짓이면 안 됨(파서 책임)
    level: Optional[int] = Field(default=None, ge=0)
    order_index: int = Field(ge=0)  # I2: 전순서
    # 타입별 페이로드: table이면 TableContent, 아니면 text
    text: Optional[str] = None
    table_content: Optional[TableContent] = None
    confidence: float = Field(ge=0.0, le=1.0)
    is_noise: bool = False
    page_ref: Union[int, list[int], None] = None

    @model_validator(mode="after")
    def _payload_matches_type(self) -> "Block":
        # I3: table 블록은 반드시 셀 구조. 그 외는 table_content 금지.
        if self.block_type == BlockType.table:
            if self.table_content is None:
                raise ValueError("table block must carry table_content (string tables are a violation)")
            if self.text is not None:
                raise ValueError("table block must not use flat text payload")
        else:
            if self.table_content is not None:
                raise ValueError(f"{self.block_type} block must not carry table_content")
            if self.text is None:
                raise ValueError(f"{self.block_type} block requires text payload")
        # header_footer는 노이즈로 표시되어야 청킹이 건너뜀
        if self.block_type == BlockType.header_footer and not self.is_noise:
            raise ValueError("header_footer block must be flagged is_noise=true")
        return self


# ── 문서 레벨 ───────────────────────────────────────────
class SourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_system: str
    department: str
    security_level: str          # FR-1에서 확정. 메타데이터 스키마 acl로 전파.
    original_path: str


class ParseQuality(BaseModel):
    """PoC 층1 지표가 그대로 들어온다. self-critique 2(폴백 발동률)도 여기서 집계."""
    model_config = ConfigDict(extra="forbid")
    char_count: int = Field(ge=0)
    table_recovery_avg: float = Field(ge=0.0, le=1.0)  # 문서 내 표 복원율 평균
    broken_char_ratio: float = Field(ge=0.0, le=1.0)
    order_confidence: float = Field(ge=0.0, le=1.0)


class DocumentIR(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION   # 1번 필드: 버전
    document_id: str                       # 멱등 키(해시 기반)
    source_format: SourceFormat
    extraction_path: ExtractionPath
    parser_version: str
    ingested_at: datetime
    source_metadata: SourceMetadata
    parse_quality: ParseQuality
    parse_status: ParseStatus
    blocks: list[Block]

    @model_validator(mode="after")
    def _document_invariants(self) -> "DocumentIR":
        # I1: block_id 유일
        ids = [b.block_id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("block_id must be unique within document (I1)")
        # I2: order_index 전순서(중복 금지)
        orders = [b.order_index for b in self.blocks]
        if len(orders) != len(set(orders)):
            raise ValueError("order_index must be a total order, no duplicates (I2)")
        # failed가 아닌데 블록이 비면 모순
        if self.parse_status != ParseStatus.failed and not self.blocks:
            raise ValueError("non-failed document must have at least one block")
        return self
