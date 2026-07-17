# Redis 설계 (Streams 인제스트)

PDF 바이트는 Redis에 넣지 않는다. API는 스풀 경로만 enqueue하고, 워커가 파일을 읽어 인덱싱한다.

---

## 키 스키마 (`REDIS_KEY_PREFIX`, 기본 `harag:`)

| 키 | 타입 | 용도 |
|----|------|------|
| `{prefix}ingest:jobs` | Stream | 인제스트 작업 (`XADD` / `XREADGROUP`) |
| `{prefix}ingest:dead` | Stream | DLQ (`MAXLEN ~ 1000`) |
| `{prefix}ingest:inflight` | Set | document_id 멱등(처리 중) |
| `{prefix}doc:{owner}:{id}` | String(JSON) | 문서 상태 캐시 TTL **300s** |
| `{prefix}rl:{owner}` | Hash | 토큰버킷 (`tokens`, `last`) |
| `{prefix}conv:{id}` | List | 대화 질의 이력 |

Consumer group: `harag-workers`.

---

## 인제스트 흐름

```text
API  → Lua(SISMEMBER inflight? / SADD + XADD)  → Stream
Worker → XAUTOCLAIM(idle>visibility) 또는 XREADGROUP >
       → PdfIngestPipeline
       → 성공/논리실패: XACK + SREM inflight
       → 예외: attempts++ 재 XADD 또는 DLQ
```

- **at-least-once**: `BRPOP` 대신 PEL + `XAUTOCLAIM`(`INGEST_VISIBILITY_SEC`, 기본 120).
- **원자 enqueue**: in-flight SET과 `XADD`를 한 Lua 스크립트로 묶음.
- **failed 후 재업로드**: 파이프라인 `_fail` → `on_failed`로 in-flight 해제. enqueue 거절 시 API가 clear 후 1회 재시도. 이미 `ready`인 중복만 스풀 삭제(`processing`+enqueue 실패는 Redis 장애 가능 → 스풀 보존).

---

## 환경변수

| 변수 | 기본 | 설명 |
|------|------|------|
| `REDIS_URL` | (빈값) | 비우면 인메모리/ThreadPool 폴백 |
| `REDIS_KEY_PREFIX` | `harag:` | 키 접두사 |
| `REDIS_STREAM_MAXLEN` | `10000` | jobs 스트림 approximate trim |
| `INGEST_VISIBILITY_SEC` | `120` | stale PEL reclaim |
| `INGEST_MAX_ATTEMPTS` | `3` | 이후 DLQ |
| `HARAG_SPOOL_DIR` | (필수 if Redis) | API·워커 공유 스풀. 없으면 API fail-fast |

클라이언트: `socket_connect_timeout=2`, `socket_timeout=5`, `health_check_interval=30`, `max_connections=20`. 연결 실패는 30초 negative cache 후 재시도(영구 off 아님).

---

## 관측

`GET /health` → `redis`: `{enabled, ok, stream, depth, pending, dead, inflight}`.

---

## 요구 버전

Redis 6.2+ (`XAUTOCLAIM`). Compose 이미지는 `redis:7-alpine`.
