"""
평가용 샘플 보험약관 PDF 생성기(의존성 없음).

make_sample_pdf.py와 같은 원리로 최소 PDF를 직접 조립하되,
  - 여러 페이지
  - 괘선(grid line)이 있는 표 → pdfplumber의 lines 전략 표 추출이 동작
를 지원한다. 내장 Helvetica는 라틴 문자만 렌더 가능하므로 본문은 영어다
(실제 한글 PDF는 임베드 폰트를 가져 pdfplumber가 한글을 정상 추출한다).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass
class Table:
    """괘선 표. x0=왼쪽 여백, y_top=표 상단(y 좌표), col_widths=열 너비."""
    headers: list[str]
    rows: list[list[str]]
    x0: float = 72.0
    y_top: float = 620.0
    col_widths: list[float] = field(default_factory=lambda: [150.0, 180.0, 120.0])
    row_height: float = 22.0


@dataclass
class Page:
    lines: list[str]
    table: Table | None = None


def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _text_ops(lines: list[str], x: float = 72, y: float = 740,
              size: int = 11, leading: int = 16) -> str:
    ops = f"BT /F1 {size} Tf {x} {y} Td {leading} TL\n"
    for i, ln in enumerate(lines):
        ops += f"({_esc(ln)}) Tj\n" if i == 0 else f"T* ({_esc(ln)}) Tj\n"
    ops += "ET\n"
    return ops


def _table_ops(t: Table) -> str:
    n_rows = 1 + len(t.rows)
    xs = [t.x0]
    for w in t.col_widths:
        xs.append(xs[-1] + w)
    y_bot = t.y_top - n_rows * t.row_height

    ops = "0.7 w\n"
    for x in xs:  # 세로 괘선
        ops += f"{x:.1f} {y_bot:.1f} m {x:.1f} {t.y_top:.1f} l S\n"
    for r in range(n_rows + 1):  # 가로 괘선
        y = t.y_top - r * t.row_height
        ops += f"{xs[0]:.1f} {y:.1f} m {xs[-1]:.1f} {y:.1f} l S\n"

    all_rows = [t.headers] + t.rows
    for r_idx, row in enumerate(all_rows):
        y_text = t.y_top - r_idx * t.row_height - 15
        for c_idx, cell in enumerate(row):
            x_text = xs[c_idx] + 4
            ops += (f"BT /F1 10 Tf {x_text:.1f} {y_text:.1f} Td "
                    f"({_esc(cell)}) Tj ET\n")
    return ops


def make_pdf(path: str, pages: list[Page]) -> None:
    objects: dict[int, bytes] = {}
    n_pages = len(pages)
    # 오브젝트 배치: 1=catalog, 2=pages, 3=font, 이후 페이지마다 (page, content)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>").encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i, pg in enumerate(pages):
        content = _text_ops(pg.lines)
        if pg.table is not None:
            content += _table_ops(pg.table)
        content_bytes = content.encode("latin-1")
        pid, cid = page_ids[i], page_ids[i] + 1
        objects[pid] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode()
        objects[cid] = (b"<< /Length %d >>\nstream\n" % len(content_bytes)
                        + content_bytes + b"\nendstream")

    buf = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(buf)
        buf += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"

    n_obj = max(objects) + 1
    xref_off = len(buf)
    buf += b"xref\n0 %d\n0000000000 65535 f \n" % n_obj
    for n in range(1, n_obj):
        buf += ("%010d 00000 n \n" % offsets[n]).encode()
    buf += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n" % n_obj
            + str(xref_off).encode() + b"\n%%EOF")

    with open(path, "wb") as f:
        f.write(buf)


# ── 평가용 샘플 문서: 건강보험 약관(영문 표본) ──
POLICY_PAGES = [
    Page(lines=[
        "Sample Health Insurance Policy",
        "",
        "Article 1 (Purpose) This policy provides for the payment of medical",
        "benefits to the insured person for disease or injury.",
        "",
        "Article 2 (Definitions) The term insured means the person named in the",
        "policy schedule. The term copayment means the share of medical expenses",
        "paid by the insured out of pocket.",
    ]),
    Page(lines=[
        "Article 3 (Coverage) The insurer pays hospitalization expenses and",
        "outpatient expenses incurred for the treatment of disease or injury,",
        "subject to the limits in Article 5.",
        "",
        "Article 4 (Claims) A claim for benefits must be filed within 30 days of",
        "the treatment date. Required documents are the claim form, medical",
        "receipts, and a diagnosis certificate issued by the attending physician.",
        "Benefits are paid within 10 business days after the claim is received.",
    ]),
    Page(
        lines=[
            "Article 5 (Benefit Limits) Annual benefit limits and copayment rates",
            "are shown in the table below. The lifetime maximum benefit under this",
            "policy is 100 million KRW.",
        ],
        table=Table(
            headers=["Item", "Annual Limit", "Copayment"],
            rows=[
                ["Outpatient", "200,000 KRW per visit", "30 percent"],
                ["Inpatient", "50,000,000 KRW", "20 percent"],
                ["Dental", "1,000,000 KRW", "50 percent"],
            ],
            y_top=640.0,
        ),
    ),
    Page(lines=[
        "Article 6 (Exclusions) The insurer does not pay benefits for cosmetic",
        "surgery, routine health checkups, or injuries caused intentionally by",
        "the insured. Treatment received outside Korea is excluded unless prior",
        "approval is obtained from the insurer.",
        "",
        "Article 7 (Termination) The policyholder may terminate this policy at",
        "any time by written notice. Premiums for the unexpired period are",
        "refunded on a pro rata basis.",
    ]),
]


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_health_policy.pdf"
    make_pdf(out, POLICY_PAGES)
    print(f"wrote {out}")
