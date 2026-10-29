"""증상 사전 — 구어체를 닫힌 증상 코드로 매핑한다."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json


_KB_PATH = Path(__file__).with_name("kb.yaml")


def load_kb() -> dict:
    return json.loads(_KB_PATH.read_text(encoding="utf-8"))


def ref_conditions_of(spec: dict | None) -> list[dict]:
    """KB 증상의 닫힌 참고 상병. 당뇨·고혈압 코드를 여기서 만들지 않는다."""
    out: list[dict] = []
    for raw in (spec or {}).get("ref_conditions") or []:
        kcd = str(raw.get("kcd") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not kcd or not name:
            continue
        out.append({
            "kcd": kcd,
            "name": name,
            "source": str(raw.get("source") or "").strip(),
        })
    return out


def portal_overview_of(spec: dict | None) -> dict | None:
    """질병관리청 포털 개요 큐레이션. 없으면 None."""
    raw = (spec or {}).get("portal_overview")
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    source = str(raw.get("source") or "").strip()
    if not text:
        return None
    return {"text": text, "source": source}


def symptom_payload(code: str, label: str, typ: str, spec: dict | None) -> dict:
    payload = {
        "code": code,
        "label": label,
        "type": typ,
        "ref_conditions": ref_conditions_of(spec),
    }
    overview = portal_overview_of(spec)
    if overview:
        payload["portal_overview"] = overview
    return payload


@dataclass(frozen=True)
class SymptomHit:
    code: str
    label: str
    alias: str
    type: str = "usual"


def match_symptoms(text: str, kb: dict | None = None) -> list[SymptomHit]:
    """마스킹된 텍스트에서 증상 코드를 찾는다. 긴 별칭을 우선한다."""
    kb = kb or load_kb()
    hay = text or ""
    hits: list[SymptomHit] = []
    seen: set[str] = set()
    candidates: list[tuple[int, str, str, str]] = []
    for code, spec in kb.get("symptoms", {}).items():
        label = spec["label"]
        for alias in spec.get("aliases") or []:
            if alias:
                candidates.append((len(alias), code, label, alias))
    for _, code, label, alias in sorted(candidates, reverse=True):
        if code in seen:
            continue
        if alias in hay:
            seen.add(code)
            spec = (kb.get("symptoms") or {}).get(code) or {}
            hits.append(SymptomHit(
                code=code, label=label, alias=alias,
                type=str(spec.get("type") or "usual")))
    return hits
