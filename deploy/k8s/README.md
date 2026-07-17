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
