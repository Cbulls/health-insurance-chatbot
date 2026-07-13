"""
Qdrant 매핑 검증 — 국면 B 요구사항이 실제 Qdrant 엔진에서 작동하는지 증명.
인메모리 모드(:memory:)로 실제 엔진을 띄운다(목 아님).

검증 항목:
  V1. named vectors(dense+sparse)로 하이브리드 컬렉션 구성
  V2. 한국어 형태소 토큰을 sparse vector로 주입(BM25 의미) — 외부 토크나이저 전제
  V3. ACL payload 필터가 검색 시점에 적용(누수 B: pre-filter, HNSW 탐색 중)
  V4. Universal Query API + RRF로 dense/sparse 단일 호출 융합(누수 A: 단일 컬렉션)
  V5. B-3 원자적 버전 전환을 payload version 필터로 구현 — 빈 창 없음
"""
from qdrant_client import QdrantClient, models

PASS, FAIL = [], []
def ok(n, cond):
    (PASS if cond else FAIL).append(n)

DENSE_DIM = 8   # 검증용 소형 차원(실제론 KURE/BGE-M3 1024)
COLL = "admin_docs"

client = QdrantClient(":memory:")

# ── V1: 하이브리드 컬렉션(named vectors) ──
client.create_collection(
    collection_name=COLL,
    vectors_config={"dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)},
    sparse_vectors_config={"sparse": models.SparseVectorParams()},
)
ok("V1/hybrid collection (dense+sparse named vectors)", client.collection_exists(COLL))

# ── ACL payload 인덱스(필터 성능) ──
client.create_payload_index(COLL, "acl_tags", models.PayloadSchemaType.KEYWORD)
client.create_payload_index(COLL, "document_id", models.PayloadSchemaType.KEYWORD)
client.create_payload_index(COLL, "version", models.PayloadSchemaType.INTEGER)
client.create_payload_index(COLL, "active", models.PayloadSchemaType.BOOL)

# ── 한국어 형태소 sparse 토큰을 정수 인덱스로 매핑(V2) ──
# 실제로는 Kiwi/Mecab 토큰 -> 사전 id. 여기선 의미만 검증.
VOCAB = {"여비": 1, "출장비": 2, "한도": 3, "국내": 4, "지급": 5, "연차": 6, "휴가": 7}
def sparse_of(tokens):
    idx = [VOCAB[t] for t in tokens if t in VOCAB]
    return models.SparseVector(indices=idx, values=[1.0]*len(idx))

def dense_of(seed):
    # 결정적 더미 dense 벡터(검증용)
    return [((seed * (i+1)) % 7) / 7.0 for i in range(DENSE_DIM)]

# ── 문서 d1 버전1 적재: 출장비 규정(finance 권한) ──
# B-3: version 필드 + active 플래그. 적재 시 active=True.
client.upsert(COLL, points=[
    models.PointStruct(id=1, vector={
        "dense": dense_of(2),
        "sparse": sparse_of(["여비", "출장비", "한도", "국내"]),
    }, payload={"document_id": "d1", "version": 1, "active": True,
                "acl_tags": ["dept:finance"], "text": "국내출장 여비 한도는 1일 5만원"}),
    # hr 권한 문서(연차) — 권한 분리 검증용
    models.PointStruct(id=2, vector={
        "dense": dense_of(6),
        "sparse": sparse_of(["연차", "휴가", "지급"]),
    }, payload={"document_id": "d2", "version": 1, "active": True,
                "acl_tags": ["dept:hr"], "text": "연차휴가는 15일"}),
])

fin_filter = models.Filter(must=[
    models.FieldCondition(key="acl_tags", match=models.MatchAny(any=["dept:finance"])),
    models.FieldCondition(key="active", match=models.MatchValue(value=True)),
])

# ── V3+V4: 하이브리드 검색 + ACL pre-filter(단일 호출) ──
res = client.query_points(
    collection_name=COLL,
    prefetch=[
        models.Prefetch(query=sparse_of(["출장비", "한도"]), using="sparse",
                        filter=fin_filter, limit=20),
        models.Prefetch(query=dense_of(2), using="dense",
                        filter=fin_filter, limit=20),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    limit=10,
).points
got_ids = {p.id for p in res}
ok("V4/hybrid RRF single-call returns finance doc", 1 in got_ids)
ok("V3/누수B ACL pre-filter excludes hr doc", 2 not in got_ids)

# hr 사용자로 같은 검색 -> finance 문서 안 보임(누수 차단 대칭 확인)
hr_filter = models.Filter(must=[
    models.FieldCondition(key="acl_tags", match=models.MatchAny(any=["dept:hr"])),
    models.FieldCondition(key="active", match=models.MatchValue(value=True)),
])
res_hr = client.query_points(COLL, prefetch=[
    models.Prefetch(query=sparse_of(["출장비", "한도"]), using="sparse", filter=hr_filter, limit=20),
    models.Prefetch(query=dense_of(2), using="dense", filter=hr_filter, limit=20),
], query=models.FusionQuery(fusion=models.Fusion.RRF), limit=10).points
ok("V3/누수B hr user cannot see finance doc", 1 not in {p.id for p in res_hr})


# ════════ V5: B-3 원자적 버전 전환을 Qdrant로 ════════
# 시나리오: d1이 개정됨(버전2). 빈 창 없이 전환.
# 1) 버전2를 active=False로 stage(검색에 안 보임 — active=True 필터 때문)
client.upsert(COLL, points=[
    models.PointStruct(id=101, vector={
        "dense": dense_of(3),
        "sparse": sparse_of(["여비", "출장비", "한도", "국내", "지급"]),
    }, payload={"document_id": "d1", "version": 2, "active": False,
                "acl_tags": ["dept:finance"], "text": "국내출장 여비 한도는 1일 7만원(개정)"}),
])

def search_d1_active():
    r = client.query_points(COLL, prefetch=[
        models.Prefetch(query=dense_of(2), using="dense", filter=models.Filter(must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value="d1")),
            models.FieldCondition(key="active", match=models.MatchValue(value=True)),
        ]), limit=20),
    ], query=models.FusionQuery(fusion=models.Fusion.RRF), limit=10).points
    return [(p.id, p.payload["version"]) for p in r]

# stage 직후: 아직 버전1만 활성(버전2는 숨김). 빈 창 없음.
staged = search_d1_active()
ok("V5/after stage: only v1 visible (no empty window)",
   staged == [(1, 1)])

# 2) 원자적 전환: set_payload로 두 연산을 적용
#    (Qdrant set_payload는 포인트 단위 원자적; 운영에선 배치/조건부로 묶음)
client.set_payload(COLL, payload={"active": True},
                   points=models.Filter(must=[
                       models.FieldCondition(key="document_id", match=models.MatchValue(value="d1")),
                       models.FieldCondition(key="version", match=models.MatchValue(value=2)),
                   ]))
client.set_payload(COLL, payload={"active": False},
                   points=models.Filter(must=[
                       models.FieldCondition(key="document_id", match=models.MatchValue(value="d1")),
                       models.FieldCondition(key="version", match=models.MatchValue(value=1)),
                   ]))

switched = search_d1_active()
ok("V5/after switch: only v2 visible (no empty, no dup)",
   switched == [(101, 2)])

# 3) GC: 비활성(버전1) 제거. 활성은 안 건드림.
client.delete(COLL, points_selector=models.Filter(must=[
    models.FieldCondition(key="document_id", match=models.MatchValue(value="d1")),
    models.FieldCondition(key="active", match=models.MatchValue(value=False)),
]))
after_gc = search_d1_active()
ok("V5/after gc: v2 still active, v1 purged", after_gc == [(101, 2)])


print(f"\n{'='*60}")
print(f"PASS: {len(PASS)} / {len(PASS)+len(FAIL)}")
for n in PASS: print("  OK", n)
for f in FAIL: print("  X ", f)
print('='*60)
