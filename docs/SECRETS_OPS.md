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

## OIDC SSO redirect 등록

창구 **기관 계정으로 로그인**은 authorization code 흐름을 씁니다.

| 변수 | 예 |
|------|-----|
| `AUTH_OIDC_AUTHORIZE_URL` | IdP authorize endpoint |
| `AUTH_OIDC_TOKEN_URL` | IdP token endpoint |
| `AUTH_OIDC_CLIENT_ID` / `CLIENT_SECRET` | 앱 등록 값 |
| `AUTH_OIDC_REDIRECT_URI` | `https://<서비스호스트>/v1/auth/callback` |
| `AUTH_OIDC_JWKS_URL` | 액세스/ID 토큰 검증용 JWKS (또는 `AUTH_JWT_SECRET` HS256) |

IdP 콘솔에 **Redirect URI**를 위 `AUTH_OIDC_REDIRECT_URI`와 동일하게 등록합니다.  
파일럿만: `AUTH_OIDC_MOCK=true` + `AUTH_JWT_SECRET` → `/v1/auth/login`이 모의 JWT를 발급(실 IdP 불필요).

역할 클레임: JWT `roles`에 `admin` 또는 `doc_admin` → 공용 규정 등록·삭제. `dept`/`departments` → 부서 공유 범위.

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
