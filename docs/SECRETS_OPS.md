# 시크릿·운영 프로파일

## 로컬

1. `cp .env.example .env` — 키는 git에 올리지 않음
2. 유출 시 즉시 재발급

## 운영 (권장)

| 항목 | 위치 |
|------|------|
| `LLM_API_KEY` / `EMBEDDING_API_KEY` | Secret Manager / K8s Secret |
| `AUTH_JWT_SECRET` 또는 OIDC | IdP + `AUTH_OIDC_JWKS_URL` |
| `OBJECT_STORE_*` | MinIO/S3 IAM |
| `DATABASE_URL` | Postgres 시크릿 |

운영 시 `AUTH_ALLOW_DEMO_OWNER=false`.

## Compose 프로파일

```bash
# 기본 MVP
docker compose up --build

# Postgres
docker compose --profile postgres up -d

# MinIO 원본 보존
docker compose --profile minio up -d

# 운영 묶음(postgres + minio)
docker compose --profile ops up --build

# TEI 리랭커(GPU 호스트)
docker compose --profile tei up -d
```
