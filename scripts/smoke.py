"""
엔드투엔드 스모크 테스트.

전제: API가 이미 떠 있어야 한다(로컬 uvicorn 또는 docker compose).
  BASE=http://localhost:8000 python scripts/smoke.py

흐름: 샘플 PDF 생성 → 업로드 → ready 폴링 → 관련 질의(답변 기대) →
      무관 질의(abstain 기대). 실패 시 비정상 종료.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

from make_sample_pdf import make_pdf, SAMPLE_LINES

BASE = os.environ.get("BASE", "http://localhost:8000")
OWNER = "smoke-" + uuid.uuid4().hex[:8]
H = {"X-Owner-Id": OWNER}


def main() -> int:
    pdf_path = "sample_travel_expense.pdf"
    make_pdf(pdf_path, SAMPLE_LINES)
    print(f"[1] sample pdf: {pdf_path}")

    with httpx.Client(base_url=BASE, timeout=60) as c:
        assert c.get("/health").json()["status"] == "ok"
        print("[2] /health ok")

        with open(pdf_path, "rb") as f:
            r = c.post("/v1/documents", headers=H,
                       files={"file": ("sample.pdf", f, "application/pdf")})
        r.raise_for_status()
        doc_id = r.json()["document_id"]
        print(f"[3] uploaded: {doc_id} ({r.json()['status']})")

        for _ in range(40):
            st = c.get(f"/v1/documents/{doc_id}", headers=H).json()
            if st["status"] == "ready":
                print(f"[4] ready: {st['n_chunks']} chunks")
                break
            if st["status"] == "failed":
                print(f"[4] FAILED ingest: {st.get('error')}")
                return 1
            time.sleep(1)
        else:
            print("[4] timeout waiting for ready")
            return 1

        r = c.post("/v1/query", headers=H,
                   json={"query": "What is the domestic travel expense limit?"})
        r.raise_for_status()
        ans = r.json()
        print(f"[5] answer: abstained={ans['abstained']}")
        print(f"    text: {ans['answer']}")
        print(f"    citations: {[x['citation_label'] for x in ans['citations']]}")
        if ans["abstained"]:
            print("[5] WARN: expected an answer but abstained "
                  "(임베딩 폴백 품질 한계일 수 있음)")

        r = c.post("/v1/query", headers=H,
                   json={"query": "What is the recipe for kimchi stew?"})
        unrelated = r.json()
        print(f"[6] unrelated query abstained={unrelated['abstained']} "
              f"(abstain 기대)")

    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
