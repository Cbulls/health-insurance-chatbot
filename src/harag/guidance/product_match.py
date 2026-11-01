"""상품 보장 태그 — axis/nature 정규화와 입력 연결 라인 (결정론)."""
from __future__ import annotations

import re
from typing import Any

from harag.guidance.coverage_priority import load_priority_rules

PREMIUM_NOTE = (
    "보험료는 나이·성별·담보·고지 유형에 따라 달라 보험사 공시나 상담에서 확인하세요."
)

_PREMIUM_FAIL_RE = re.compile(
    r"[^.]*?(월납\s*예시|공개\s*월납|찾기\s*어려워)[^.]*\.\s*",
    re.I,
)
_CHRONIC_PITCH_RE = re.compile(
    r"유병력자[·\s]*고령층\s*친화\s*",
)
_CHRONIC_ALSO_RE = re.compile(r"유병력자도\s*")

# 청크 텍스트에서 축을 찾을 때. 정액성 단어는 lump 전용.
INDEMNITY_ALIASES: dict[str, list[str]] = {
    "outpatient": ["통원의료비", "상해통원", "질병통원", "실손 통원", "실손통원"],
    "inpatient": ["입원의료비", "상해입원", "질병입원", "실손 입원", "실손입원"],
    "tests": ["검사비", "실손 검사", "실손검사"],
    "injury": ["일반상해", "상해의료비", "교통상해", "상해후유장해", "상해 관련 실손"],
}
LUMP_ALIASES: dict[str, list[str]] = {
    "outpatient": ["외래일당", "통원일당"],
    "inpatient": ["입원일당", "입원비 정액"],
    "tests": ["골절진단비", "화상진단비", "진단비"],
    "injury": ["상해수술비", "상해사망"],
    "surgery": ["수술비", "수술특약", "질병수술", "상해수술"],
}

CODE_TO_AXIS = {
    "outpatient": "outpatient",
    "inpatient": "inpatient",
    "tests": "tests",
    "injury": "injury",
}

LABEL_TO_AXIS = {
    "실손 통원": "outpatient",
    "실손 입원": "inpatient",
    "실손 검사": "tests",
    "상해 관련 실손": "injury",
    "정액 통원·외래": "outpatient",
    "정액 입원일당": "inpatient",
    "정액 진단비": "tests",
    "정액 수술비": "surgery",
    "상해 정액": "injury",
}


def display_title(axis: str, nature: str) -> str:
    rules = load_priority_rules()
    titles = rules.get("display_titles") or {}
    key = f"{axis}|{nature}"
    return str(titles.get(key) or f"{axis}/{nature}")


def product_nature(cost_nature: str | None) -> str:
    c = (cost_nature or "").strip()
    if c == "lump_sum":
        return "lump_sum"
    if c == "indemnity":
        return "indemnity"
    if c == "mixed":
        return "mixed"
    return ""


def _axes_from_codes(codes: list | None) -> set[str]:
    out: set[str] = set()
    for code in codes or []:
        ax = CODE_TO_AXIS.get(str(code))
        if ax:
            out.add(ax)
    return out


def _axes_from_labels(labels: list | None) -> set[str]:
    out: set[str] = set()
    for lab in labels or []:
        ax = LABEL_TO_AXIS.get(str(lab))
        if ax:
            out.add(ax)
        else:
            # 느슨: 문자열에 축 키워드
            s = str(lab)
            if "수술" in s:
                out.add("surgery")
            elif "통원" in s or "외래" in s:
                out.add("outpatient")
            elif "입원" in s:
                out.add("inpatient")
            elif "진단" in s or "검사" in s:
                out.add("tests")
            elif "상해" in s:
                out.add("injury")
    return out


def _blob(p: dict) -> str:
    parts = [
        str(p.get("name") or ""),
        str(p.get("title") or ""),
        str(p.get("summary") or ""),
        str(p.get("snippet") or ""),
        " ".join(str(x) for x in (p.get("matched_coverages") or [])),
    ]
    return " ".join(parts)


def axes_present(p: dict) -> set[str]:
    found = _axes_from_codes(p.get("coverage_codes"))
    found |= _axes_from_labels(p.get("matched_coverages"))
    blob = _blob(p)
    for axis, aliases in LUMP_ALIASES.items():
        if any(a in blob for a in aliases):
            found.add(axis)
    for axis, aliases in INDEMNITY_ALIASES.items():
        if any(a in blob for a in aliases):
            found.add(axis)
    return found


def normalize_matched_tags(products: list[dict]) -> list[dict]:
    """cost_nature에 맞게 matched_coverages 라벨을 실손/정액으로 정규화."""
    out: list[dict] = []
    for p in products or []:
        row = dict(p)
        nature = product_nature(row.get("cost_nature"))
        axes = axes_present(row)
        labels: list[str] = []
        tag_nature = nature if nature in ("lump_sum", "indemnity", "mixed") else ""
        if not tag_nature:
            old = [str(x) for x in (row.get("matched_coverages") or [])]
            blob = _blob(row)
            if any(x.startswith("실손") for x in old):
                tag_nature = "indemnity"
            elif any(
                a in blob
                for aliases in LUMP_ALIASES.values()
                for a in aliases
            ):
                tag_nature = "lump_sum"
        if tag_nature == "mixed":
            for ax in sorted(axes):
                if ax == "surgery":
                    labels.append(display_title(ax, "lump_sum"))
                else:
                    labels.append(display_title(ax, "indemnity"))
        elif tag_nature in ("lump_sum", "indemnity"):
            for ax in sorted(axes):
                if ax == "surgery" and tag_nature == "indemnity":
                    labels.append(display_title("inpatient", "indemnity"))
                    continue
                n = (
                    "lump_sum"
                    if ax == "surgery" and tag_nature == "indemnity"
                    else tag_nature
                )
                labels.append(display_title(ax, n))
        else:
            for ax in sorted(axes):
                labels.append(display_title(ax, "either"))
        # 안정 순서 + 중복 제거
        seen: set[str] = set()
        clean: list[str] = []
        for lab in labels:
            if lab and lab not in seen:
                seen.add(lab)
                clean.append(lab)
        row["matched_coverages"] = clean
        row["matched_axes"] = sorted(axes)
        out.append(row)
    return out


def _nature_compatible(product_cost: str, want: str) -> str:
    """ok | nature_mismatch | unknown."""
    pn = product_nature(product_cost)
    if want == "either":
        return "ok"
    if not pn:
        return "unknown"
    if pn == "mixed":
        return "ok"
    if want == pn:
        return "ok"
    return "nature_mismatch"


def build_input_link_lines(
        product: dict,
        coverage_priority: dict | None) -> list[dict[str, Any]]:
    """④ 순위 항목 vs 상품 → ✓/✗/미확인 라인."""
    items = (coverage_priority or {}).get("items") or []
    if not items:
        return []
    axes = axes_present(product)
    cost = str(product.get("cost_nature") or "")
    lines: list[dict[str, Any]] = []
    for it in items:
        axis = str(it.get("axis") or "")
        want = str(it.get("nature") or "either")
        title = str(it.get("title") or display_title(axis, want))
        has = axis in axes
        if axis == "surgery" and not has:
            # 수술 축은 coverage_codes에 없을 수 있음
            has = any(a in _blob(product) for a in LUMP_ALIASES.get("surgery", []))

        if not has:
            # 미확인·유사 칩은 사용자에게 노출하지 않음
            continue

        if _nature_compatible(cost, want) == "ok":
            lines.append({
                "axis": axis,
                "nature": want,
                "label": title,
                "status": "match",
                "text": f"{title} ✓",
            })
        # nature_mismatch / unknown 라인은 생성하지 않음
    return lines


def attach_input_links(
        products: list[dict],
        coverage_priority: dict | None) -> list[dict]:
    out = []
    for p in products or []:
        row = dict(p)
        row["input_links"] = build_input_link_lines(row, coverage_priority)
        out.append(row)
    return out


def soften_product_summary(summary: str) -> str:
    """유병력 마케팅·월납 한계 고백 문장을 사용자 안내용으로 정리."""
    text = str(summary or "").strip()
    if not text:
        return ""
    text = _PREMIUM_FAIL_RE.sub("", text)
    text = _CHRONIC_PITCH_RE.sub("", text)
    text = _CHRONIC_ALSO_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"\s+\.", ".", text)
    return text


def extract_watch_points(summary: str, cost_nature: str | None = None) -> list[str]:
    """요약서 문구에서 알아둘 점 2~3개 (키워드 기반)."""
    text = str(summary or "")
    points: list[str] = []
    if "갱신" in text:
        points.append("갱신형이라 갱신 때 보험료가 오를 수 있어요")
    if "해약환급금 미지급" in text or "환급금이 없" in text or "환급이 없" in text:
        points.append("해약환급금이 없는 유형이면 중도 해지 시 돌려받지 못할 수 있어요")
    if "면책" in text or "감액" in text:
        points.append("암 등 일부 보장은 면책·감액 기간이 있을 수 있어요 (상품별 확인)")
    if "간편고지" in text or "간편심사" in text:
        points.append(
            "간편고지·일반고지 유형에 따라 가입 조건·보험료가 달라질 수 있어요"
        )
    if (cost_nature or "") == "lump_sum" and not any("정액" in p for p in points):
        points.append("정액형은 실제 의료비가 아니라 약정 금액을 지급하는 구조예요")
    # 안정적 상위 3
    seen: set[str] = set()
    out: list[str] = []
    for p in points:
        if p not in seen:
            seen.add(p)
            out.append(p)
        if len(out) >= 3:
            break
    return out


def structure_product_copy(product: dict) -> dict:
    """short_summary · watch_points · premium_note 부여."""
    row = dict(product)
    raw = str(row.get("summary") or "")
    soft = soften_product_summary(raw)
    # 한 줄: 첫 문장
    first = soft.split(".")[0].strip()
    if first and not first.endswith("요") and not first.endswith("다"):
        short = first + "."
    elif first:
        short = first + ("." if not first.endswith(".") else "")
    else:
        short = ""
    if len(short) > 140:
        short = short[:137] + "…"
    row["summary"] = soft or raw
    row["short_summary"] = short
    row["watch_points"] = extract_watch_points(soft or raw, row.get("cost_nature"))
    row["premium_note"] = PREMIUM_NOTE
    return row


def enrich_product_presentation(
        products: list[dict],
        coverage_priority: dict | None = None) -> list[dict]:
    tagged = attach_input_links(products, coverage_priority)
    return [structure_product_copy(p) for p in tagged]
