"""
의존성 없이 최소 PDF를 생성한다(스모크 테스트용).

주의: 내장 Helvetica는 라틴 문자만 렌더 가능하므로 샘플 본문은 영어다.
실제 한글 PDF는 자체 임베드 폰트를 가지므로 pdfplumber가 한글을 정상 추출한다
(이 스크립트는 어디까지나 엔드투엔드 배관을 증명하기 위한 합성 표본).
"""
from __future__ import annotations

import sys

SAMPLE_LINES = [
    "Travel Expense Regulation (Sample)",
    "",
    "Article 12. The domestic travel expense limit is 50,000 KRW per day.",
    "Overseas travel per diem is 120 USD per day for staff.",
    "Accommodation is reimbursed up to 100,000 KRW per night.",
    "",
    "Article 13. Reimbursement requires original receipts within 14 days.",
    "Mileage for private car use is paid at 300 KRW per kilometer.",
    "",
    "Article 14. The approval authority for expenses over 1,000,000 KRW is the director.",
]


def make_pdf(path: str, lines: list[str]) -> None:
    content = "BT /F1 12 Tf 72 740 Td 16 TL\n"
    for i, ln in enumerate(lines):
        esc = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({esc}) Tj\n" if i == 0 else f"T* ({esc}) Tj\n"
    content += "ET"
    content_bytes = content.encode("latin-1")

    buf = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}

    def add(num: int, body: bytes) -> None:
        nonlocal buf
        offsets[num] = len(buf)
        buf += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"

    add(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    add(4, b"<< /Length %d >>\nstream\n" % len(content_bytes) + content_bytes
           + b"\nendstream")
    add(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    xref_off = len(buf)
    buf += b"xref\n0 6\n0000000000 65535 f \n"
    for n in range(1, 6):
        buf += ("%010d 00000 n \n" % offsets[n]).encode()
    buf += (b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
            + str(xref_off).encode() + b"\n%%EOF")

    with open(path, "wb") as f:
        f.write(buf)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_travel_expense.pdf"
    make_pdf(out, SAMPLE_LINES)
    print(f"wrote {out}")
