#!/usr/bin/env bash
# 설명서 0건 / split-brain 진단. 인프라 담당자가 재배포 직후 실행.
# 사용: HOST=https://api.example.com ./deploy/k8s/check-library.sh
#       API_DEPLOY=deploy/harag-api ./deploy/k8s/check-library.sh   # kubectl 가능 시
set -euo pipefail

HOST="${HOST:-http://127.0.0.1:8000}"
API_DEPLOY="${API_DEPLOY:-}"
NS="${NS:-}"

echo "== GET ${HOST}/health =="
health="$(curl -fsS "${HOST}/health")" || {
  echo "FAIL: /health unreachable" >&2
  exit 1
}
echo "$health" | python3 -c '
import json,sys
b=json.load(sys.stdin)
keys=("status","library_docs","library_docs_ready","split_brain","library_docs_error")
for k in keys:
    if k in b:
        print(f"  {k}: {b[k]}")
ready=int(b.get("library_docs_ready") or 0)
split=bool(b.get("split_brain"))
if split:
    print("DIAG: split_brain — Qdrant에 lib 벡터는 있는데 메타 ready=0.")
    print("  → API 재기동으로 reconcile 유도, DATABASE_URL 쓰기 가능 여부 확인.")
    sys.exit(2)
if ready == 0:
    print("DIAG: library_docs_ready=0 (cold empty 가능).")
    print("  → pod /app/demo_pdfs PDF 존재·임베딩 시크릿·demo-seed 로그 확인.")
    sys.exit(3)
print(f"OK: library_docs_ready={ready}")
'

if [[ -n "$API_DEPLOY" ]]; then
  k=(kubectl)
  [[ -n "$NS" ]] && k+=(-n "$NS")
  echo "== kubectl exec ${API_DEPLOY} — /app/demo_pdfs =="
  "${k[@]}" exec "$API_DEPLOY" -- sh -c 'ls -la /app/demo_pdfs 2>/dev/null || echo MISSING'
  n="$("${k[@]}" exec "$API_DEPLOY" -- sh -c 'ls /app/demo_pdfs/*.pdf 2>/dev/null | wc -l' | tr -d '[:space:]')"
  echo "  pdf_count=${n}"
  if [[ "${n:-0}" -eq 0 ]]; then
    echo "DIAG: 이미지/마운트에 PDF 없음 — demo_pdfs 포함 재빌드, 빈 볼륨 마운트 제거." >&2
    exit 4
  fi
  echo "== recent demo-seed / reconcile logs =="
  "${k[@]}" logs "$API_DEPLOY" --tail=200 2>/dev/null \
    | grep -E 'demo.seed|reconcile|demo_pdfs|seed_demo|library' || true
fi

echo "== done =="
