"""
B-3 동시성 증명 — 스레드를 실제로 돌려 빈 창(empty window)을 검증.
동시성 버그는 단위 테스트로 안 잡힌다. upsert 진행 중 끊임없이 검색해
'문서가 존재하는데 0건 회수'가 한 번이라도 나는지 본다.
"""
import threading
import time

from harag.indexing.versioning import VersionedStore, upsert


def stress(use_versioned: bool, iterations: int = 200):
    """
    use_versioned=True : 버전 태깅 + 원자적 전환(우리 해법)
    use_versioned=False: 순진한 DELETE-then-INSERT(대조군) — 빈 창이 나야 정상
    반환: (검색 횟수, 빈창 관측 횟수, 중복창 관측 횟수)
    """
    store = VersionedStore()
    doc = "regulation-d1"
    # 초기 버전 1 적재·활성화
    upsert(store, doc, 1, {"c1", "c2", "c3"})

    stop = threading.Event()
    empty_hits = {"n": 0}
    dup_hits = {"n": 0}
    search_count = {"n": 0}

    # 순진한 방식용 별도 저장(대조군)
    naive = {"chunks": {"c1", "c2", "c3"}, "lock": threading.Lock()}

    def searcher():
        while not stop.is_set():
            if use_versioned:
                got = store.search_dense(doc)
            else:
                with naive["lock"]:
                    got = set(naive["chunks"])
            search_count["n"] += 1
            if len(got) == 0:
                empty_hits["n"] += 1          # 빈 창! 문서는 존재하는데 0건
            elif len(got) > 4:
                dup_hits["n"] += 1            # 중복 창(옛+새 공존)

    def writer():
        for v in range(2, 2 + iterations):
            new = {f"c{v}_{i}" for i in range(4)}
            if use_versioned:
                upsert(store, doc, v, new)   # stage -> activate(원자적)
            else:
                # 순진한 방식: 삭제 후 삽입 사이에 빈 창
                with naive["lock"]:
                    naive["chunks"] = set()           # DELETE
                time.sleep(0.00005)                   # 빈 창(실제 I/O 지연 모사)
                with naive["lock"]:
                    naive["chunks"] = new             # INSERT
            time.sleep(0.00005)
        stop.set()

    threads = [threading.Thread(target=searcher) for _ in range(4)]
    w = threading.Thread(target=writer)
    for t in threads: t.start()
    w.start()
    w.join()
    for t in threads: t.join()

    return search_count["n"], empty_hits["n"], dup_hits["n"]


print("="*60)
print("대조군: 순진한 DELETE-then-INSERT")
sc, empty, dup = stress(use_versioned=False)
print(f"  검색 {sc}회 중 빈 창 관측: {empty}회  (>0 이면 self-critique 5번 재현)")
naive_bug = empty > 0

print("\n우리 해법: 버전 태깅 + 원자적 포인터 전환")
sc2, empty2, dup2 = stress(use_versioned=True)
print(f"  검색 {sc2}회 중 빈 창 관측: {empty2}회, 중복 창: {dup2}회")

print("\n" + "="*60)
ok = naive_bug and empty2 == 0 and dup2 == 0
if ok:
    print("OK 증명 완료:")
    print("  - 순진한 방식은 빈 창을 실제로 만든다(버그 재현)")
    print("  - 우리 해법은 빈 창도 중복 창도 0 (C1/C2 보장)")
else:
    print("검토 필요:")
    if not naive_bug: print("  - 대조군에서 빈 창이 안 나옴(타이밍 운; 반복 필요)")
    if empty2: print(f"  - 우리 해법에서 빈 창 {empty2}회(불변식 위반!)")
    if dup2: print(f"  - 우리 해법에서 중복 창 {dup2}회(불변식 위반!)")
print("="*60)
