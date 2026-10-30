# 진단: Qdrant(벡터) vs 메타DB (이중 층)

## 설계

| 층 | 정본 | 실패 시 |
|----|------|---------|
| Qdrant | library 청크 벡터 (RAG) | AI가 약관을 못 씀 |
| 메타DB | `scope=library` ready 행 | UI「설명서 N건」=0 |
| demo_pdfs | 콜드 시드·PDF 원본 | 벡터 없을 때만 재임베딩 필요 |

부트스트랩 (`bootstrap_library`): **reconcile → seed**.  
기동 스레드 + `POST /v1/library/bootstrap` (doc_admin).

## `/health` 필드

- `library_meta_ready` / `library_docs_ready` — 메타 ready 수  
- `library_vector_docs` — Qdrant library 고유 document 수  
- `split_brain` — `library_vector_docs > 0` 이고 meta ready == 0  
- `library_bootstrap` — `{status, reconciled, seeded, error, updated_at}`

## 로컬에서 확인된 공유 Qdrant (참고)

앱 담당 `.env`의 Cloud Qdrant가 살아 있으면(예: points>0, status green)  
「Qdrant 고장」보다 **AWS 메타/이미지** 쪽을 먼저 본다.

## AWS에서 한 방

```bash
HOST=https://<aws-api> ./deploy/k8s/check-library.sh
# 또는
curl -sS https://<aws-api>/health
# doc_admin JWT로 즉시 복구
curl -sS -X POST -H "Authorization: Bearer <token>" \
  "https://<aws-api>/v1/library/bootstrap?background=false"
```

| 결과 | 담당 |
|------|------|
| split_brain | AWS: 메타 영속 + bootstrap/재기동. Qdrant 손댈 필요 없음 |
| vector=0, PDF 없음 | AWS: demo_pdfs 이미지 |
| vector=0, PDF 있음 | 앱: 임베딩 Secret·로그 |
