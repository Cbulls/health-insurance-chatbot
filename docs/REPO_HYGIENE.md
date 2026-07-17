# 레포 위생

## 런타임 vs 아카이브

- **런타임**: `src/harag/`, `frontend/`, `tests/`, `eval/`, `scripts/`, `docker-compose.yml`, `Dockerfile`
- **설계 아카이브**: 한국어 이름 폴더(`앞으로 해야 할 것들/`, `설계/` 등) — import·compose에서 사용하지 않음

## 권장

1. 신규 코드는 루트 `src/harag`에만 추가
2. 아카이브 zip/대용량 표본은 git-lfs 또는 별도 스토리지
3. clone 비용이 커지면 아카이브 폴더를 별도 레포로 분리

## CI

루트 `.github/workflows/`만 사용한다. 아카이브 내부 workflow는 무시한다.
