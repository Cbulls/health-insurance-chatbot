# Kubernetes 골격 (Phase 4)

**새 이미지 빌드·ECR 푸시·롤아웃·검증 전체 절차:** 루트 [README.md](../../README.md) 「AWS 인프라 — 할 일」.

다중 AZ·HPA가 필요할 때 사용. 시크릿은 클러스터 Secret / External Secrets로 주입.

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment-api.yaml
kubectl apply -f deploy/k8s/deployment-worker.yaml
kubectl apply -f deploy/k8s/hpa.yaml
```

전제: 외부 Qdrant·Redis·Postgres·S3(또는 클러스터 내 매니지드) 엔드포인트가 ConfigMap/Secret에 있다.

---

## 설명서 0건 / split-brain (재배포 복구 런북)

UI「설명서 N건」은 Qdrant가 아니라 **메타DB `scope=library` + `status=ready` 행**이다.
API 기동(및 `POST /v1/library/bootstrap`, doc_admin) 시:

1. `reconcile` — Qdrant `lib:knowledge` → 메타 ready (**재임베딩 없음**)  
2. `seed` — 이미지 `/app/demo_pdfs/*.pdf` 적재  

entrypoint는 시드하지 않는다. 이미지에는 `Dockerfile`이 `COPY demo_pdfs` 한다.
`/health`: `library_meta_ready`, `library_vector_docs`, `split_brain`, `library_bootstrap`.

### 1) 원인 가르기 (5분)

```bash
# 로컬/터널
HOST=https://<host> ./deploy/k8s/check-library.sh

# 클러스터에서 PDF·로그까지
HOST=https://<host> API_DEPLOY=deploy/<api> NS=<ns> ./deploy/k8s/check-library.sh

# 수동
curl -sS https://<host>/health   # library_docs, library_docs_ready, split_brain
kubectl -n <ns> exec deploy/<api> -- ls -la /app/demo_pdfs
kubectl -n <ns> logs deploy/<api> | grep -E 'demo.seed|reconcile|demo_pdfs|seed'
```

| `/health` / 증상 | 의미 | 조치 |
|------------------|------|------|
| `split_brain: true` | Qdrant만 있고 메타 ready=0 | 최신 이미지면 기동 reconcile이 복구해야 함. 실패 시 `DATABASE_URL` 쓰기·API 재기동 |
| `library_docs_ready: 0`, `split_brain: false`, pod에 PDF 없음 | 시드 스킵 | **빌드 컨텍스트에 `demo_pdfs` 포함** 재빌드·재배포. `/app/demo_pdfs`에 **빈 볼륨 마운트 금지** |
| PDF는 있는데 ready 0 | 시드/임베딩 실패 | 임베딩·Qdrant 시크릿, `demo-seed` 로그. 시드는 **API lifespan**(워커 아님) |
| 잠깐 0 | 시드 진행 중 | 1–몇 분 후 `/health` 재확인 |

### 2) 영속화 (재발 방지)

- [ ] 메타DB: **영구 Postgres** (`DATABASE_URL`). 재배포마다 빈 SQLite/볼륨이면 매번 0건 또는 reconcile 의존
- [ ] Qdrant: URL·컬렉션(`QDRANT_COLLECTION`)이 이전과 동일해야 reconcile 동작
- [ ] 이미지: 이 레포 `Dockerfile`로 빌드, `demo_pdfs/`가 컨텍스트에 포함 (`COPY demo_pdfs`)
- [ ] `/app/demo_pdfs`에 빈 PVC/emptyDir 마운트하지 않음
- [ ] PDF 열기: 이미지 내 PDF 또는 `OBJECT_STORE_*`(S3). reconcile만으로 건수는 살아도 원본 열기는 파일/오브젝트 필요

### 3) 배포 후 합격 기준

- `/health`: `library_docs_ready` ≥ 데모 PDF 수(대략 10+), `split_brain: false`
- UI「설명서 N건」> 0
- (가능하면) 문서 목록에서 PDF 열림

```bash
curl -sS https://<host>/health
# AUTH_ALLOW_DEMO_OWNER=true 인 데모만:
curl -sS -H 'X-Owner-Id: demo' https://<host>/v1/documents
```

운영에서 `AUTH_ALLOW_DEMO_OWNER=false`이면 JWT로 `/v1/documents`를 본다.
