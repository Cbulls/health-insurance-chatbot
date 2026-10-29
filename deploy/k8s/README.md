# Kubernetes 골격 (Phase 4)

다중 AZ·HPA가 필요할 때 사용. 시크릿은 클러스터 Secret / External Secrets로 주입.

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/deployment-api.yaml
kubectl apply -f deploy/k8s/deployment-worker.yaml
kubectl apply -f deploy/k8s/hpa.yaml
```

전제: 외부 Qdrant·Redis·Postgres·S3(또는 클러스터 내 매니지드) 엔드포인트가 ConfigMap/Secret에 있다.

## 설명서 0건 / split-brain

UI「설명서 N건」은 Qdrant가 아니라 **메타DB `scope=library`** 행이다.
이미지에 `demo_pdfs`가 없고 DB가 비면 목록이 비고, Cloud Qdrant만 채워진 경우
`GET /health`에 `split_brain: true`가 뜬다. API 기동 시 `reconcile_library_from_qdrant`가
벡터→메타를 복구하며, PDF 서빙을 위해 이미지/`OBJECT_STORE_*`에 원본이 있어야 한다.

배포 후 확인:

```bash
curl -sS https://<host>/health   # library_docs_ready, split_brain
curl -sS -H 'X-Owner-Id: demo' https://<host>/v1/documents
```
