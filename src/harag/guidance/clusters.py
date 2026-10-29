"""증상 묶음(클러스터) — 닫힌 KB 매칭. LLM이 병명을 만들지 않는다."""
from __future__ import annotations

from harag.guidance.lexicon import load_kb


def match_symptom_clusters(symptom_codes: list[str] | None) -> list[dict]:
    """선택 칩 코드로 클러스터 히트를 만든다."""
    codes = {c for c in (symptom_codes or []) if c}
    if not codes:
        return []
    kb = load_kb()
    specs = kb.get("symptom_clusters") or {}
    hits: list[dict] = []
    for cid, raw in specs.items():
        if not isinstance(raw, dict):
            continue
        hit = _match_one(cid, raw, codes)
        if hit:
            hits.append(hit)
    return hits


def _match_one(cid: str, spec: dict, codes: set[str]) -> dict | None:
    require_all = {c for c in (spec.get("require_all") or []) if c}
    require_any = {c for c in (spec.get("require_any_usual") or []) if c}
    require_hidden = {c for c in (spec.get("require_hidden") or []) if c}
    min_hits = int(spec.get("min_hits") or 0)

    if require_all and not require_all <= codes:
        return None
    if require_hidden and not require_hidden <= codes:
        return None
    if require_any and not (require_any & codes):
        return None

    pool = require_all | require_any | require_hidden
    matched = sorted(codes & pool) if pool else sorted(codes)
    if min_hits and len(matched) < min_hits:
        return None
    if not matched and (require_all or require_any or require_hidden):
        return None

    families = []
    for fam in spec.get("related_families") or []:
        if not isinstance(fam, dict):
            continue
        fid = str(fam.get("id") or "").strip()
        label = str(fam.get("label") or "").strip()
        if not fid or not label:
            continue
        kcds = [
            str(k).strip() for k in (fam.get("example_kcds") or []) if str(k).strip()
        ]
        families.append({
            "id": fid,
            "label": label,
            "example_kcds": kcds,
            "rationale_source": str(fam.get("rationale_source") or "").strip(),
            "blurb": str(fam.get("blurb") or "").strip(),
        })
    if not families:
        return None
    return {
        "id": cid,
        "label": str(spec.get("label") or cid).strip(),
        "matched_symptoms": matched,
        "related_families": families,
        "care_hint": str(spec.get("care_hint") or "").strip(),
        "occupation_note": str(spec.get("occupation_note") or "").strip(),
    }


def cluster_example_kcds(clusters: list[dict]) -> list[str]:
    """클러스터 관련 KCD (중복 제거, 순서 유지)."""
    seen: set[str] = set()
    out: list[str] = []
    for cl in clusters or []:
        for fam in cl.get("related_families") or []:
            for kcd in fam.get("example_kcds") or []:
                key = str(kcd).strip().upper().replace(".", "")
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(str(kcd).strip())
    return out
