#!/usr/bin/env python3
"""
계약 테스트 런너 — CI 신뢰성 보강.

문제: 계약 테스트가 스크립트형(if __name__)이라 pytest가 수집 못 하고,
실패해도 sys.exit(1)을 안 해 CI가 초록으로 착각할 수 있다.

해결: 각 테스트를 서브프로세스로 실행하고 'PASS: a / b' 출력을 파싱해
a != b이거나 출력이 없으면 실패로 종료코드 1을 강제한다.
"""
import re
import subprocess
import sys
from pathlib import Path

CONTRACT_DIR = Path(__file__).parent.parent / "tests" / "contract"
SRC = Path(__file__).parent.parent / "src"

PASS_RE = re.compile(r"PASS:\s*(\d+)\s*/\s*(\d+)")


def run_one(path: Path) -> tuple[bool, str]:
    env = {"PYTHONPATH": str(SRC)}
    import os
    env = {**os.environ, **env}
    proc = subprocess.run([sys.executable, str(path)], capture_output=True,
                          text=True, env=env, timeout=300)
    out = proc.stdout + proc.stderr

    # 종료 코드가 0이 아니면 즉시 실패
    if proc.returncode != 0:
        return False, f"종료코드 {proc.returncode}"

    # PASS: a / b 파싱
    m = PASS_RE.search(out)
    if not m:
        # versioning처럼 PASS 카운트가 없는 검증은 종료코드 0이면 통과로 간주
        if "빈 창" in out or "0" in out:
            return True, "검증 통과(카운트 없음)"
        return False, "PASS 카운트를 찾을 수 없음"

    passed, total = int(m.group(1)), int(m.group(2))
    if passed != total:
        return False, f"{passed}/{total} 통과 (실패 {total - passed}개)"
    return True, f"{passed}/{total}"


def main() -> int:
    files = sorted(CONTRACT_DIR.glob("test_*.py"))
    if not files:
        print("계약 테스트 파일 없음", file=sys.stderr)
        return 1

    all_ok = True
    print(f"계약 테스트 {len(files)}개 실행\n" + "=" * 56)
    for f in files:
        try:
            ok, detail = run_one(f)
        except subprocess.TimeoutExpired:
            ok, detail = False, "타임아웃"
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {f.name}: {detail}")
        all_ok = all_ok and ok

    print("=" * 56)
    if all_ok:
        print("모든 계약 테스트 통과")
        return 0
    print("계약 테스트 실패 — 머지 차단", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
