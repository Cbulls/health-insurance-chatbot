"""증상 사전 — 구어체를 닫힌 증상 코드로 매핑한다."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json


_KB_PATH = Path(__file__).with_name("kb.yaml")


def load_kb() -> dict:
    return json.loads(_KB_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class SymptomHit:
    code: str
    label: str
    alias: str


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
            hits.append(SymptomHit(code=code, label=label, alias=alias))
    return hits
