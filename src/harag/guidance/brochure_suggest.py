"""설명서 추천 질문 — 카탈로그 facts + LLM 윤문 (soft no-evidence 없음)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from harag.guidance.brochure import (
    COST_NATURES, PRODUCT_LINES, catalog_entry, labels_for,
    load_brochure_catalog, sanitize_brochure_payload,
)

logger = logging.getLogger("harag.brochure_suggest")

# 프론트 DOC_ASK_SUGGEST_CANDIDATES 와 동일 문구 유지
SUGGEST_QUESTIONS: tuple[str, ...] = (
    "이 상품은 어떤 종류의 보험인가요?",
    "요약서에 안내된 주요 보장·특약은 무엇인가요?",
    "갱신·해약환급·고지 관련으로 알아둘 점은?",
)

KIND_PRODUCT_TYPE = "product_type"
KIND_COVERAGES = "coverages"
KIND_WATCH = "watch"

_AXIS_LABELS = {
    "outpatient": "통원·외래",
    "inpatient": "입원",
    "tests": "검사·진단",
    "injury": "상해",
}

_WS_RE = re.compile(r"\s+")
_DOC_MARKER_RE = re.compile(r"\[\s*문서\s*\d+(?:\s*,\s*\d+)*\s*\]")
_BAD_PARTICLE_RE = re.compile(r"은\(는\)|이\(가\)|을\(를\)|와\(과\)")
_SOLICIT_RE = re.compile(r"가입하세요|가입을 추천|가입을 권유")
_META_SUMMARY_RE = re.compile(
    r"상품요약서입니다\.?$|상품요약서입니다$",
)


@dataclass(frozen=True)
class SuggestMatch:
    kind: str
    question: str


def normalize_suggest_query(q: str) -> str:
    return _WS_RE.sub(" ", (q or "").strip())


def match_suggest_question(query: str) -> SuggestMatch | None:
    """추천 칩 질문(또는 거의 동일 문구)만 매칭."""
    nq = normalize_suggest_query(query)
    if not nq:
        return None
    for i, q in enumerate(SUGGEST_QUESTIONS):
        if nq == q:
            kind = (KIND_PRODUCT_TYPE, KIND_COVERAGES, KIND_WATCH)[i]
            return SuggestMatch(kind=kind, question=q)
    compact = nq.replace(" ", "")
    if "어떤종류의보험" in compact or (
            "종류의 보험" in nq and "어떤" in nq):
        return SuggestMatch(
            kind=KIND_PRODUCT_TYPE, question=SUGGEST_QUESTIONS[0])
    if "주요보장" in compact or "주요 보장" in nq or "특약은 무엇" in nq:
        return SuggestMatch(
            kind=KIND_COVERAGES, question=SUGGEST_QUESTIONS[1])
    if ("갱신" in nq or "해약" in nq or "고지" in nq) and (
            "알아둘" in nq or "관련" in nq):
        return SuggestMatch(
            kind=KIND_WATCH, question=SUGGEST_QUESTIONS[2])
    return None


def profile_from_catalog_filename(filename: str) -> dict | None:
    entry = catalog_entry(filename)
    if not entry:
        return None
    return labels_for(sanitize_brochure_payload(
        {**entry, "filled_by": "catalog"}, filename))


def resolve_profile_for_document(
        document_id: str, metadata=None) -> dict | None:
    """document_id → brochure labels dict (카탈로그 또는 메타 프로필)."""
    did = (document_id or "").strip()
    if not did:
        return None
    if metadata is not None:
        getter = getattr(metadata, "get_brochure_profile", None)
        if callable(getter):
            try:
                rec = getter(did)
            except Exception:  # noqa: BLE001
                rec = None
            if rec is not None:
                raw = {
                    "display_name": getattr(rec, "display_name", "") or "",
                    "product_line": getattr(rec, "product_line", "") or "",
                    "doc_kind": getattr(rec, "doc_kind", "") or "",
                    "cost_nature": getattr(rec, "cost_nature", "") or "",
                    "summary": getattr(rec, "summary", "") or "",
                    "insurer": getattr(rec, "insurer", "") or "",
                    "source_url": getattr(rec, "source_url", "") or "",
                    "coverage_codes": list(
                        getattr(rec, "coverage_codes", ()) or []),
                }
                if raw.get("summary") or raw.get("display_name"):
                    return labels_for(raw)
        list_fn = getattr(metadata, "list_library_documents", None)
        if callable(list_fn):
            try:
                rows = list_fn() or []
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                rid = getattr(row, "document_id", None)
                if rid is None and isinstance(row, dict):
                    rid = row.get("document_id")
                if str(rid or "") != did:
                    continue
                fn = getattr(row, "filename", None)
                if fn is None and isinstance(row, dict):
                    fn = row.get("filename") or row.get("source_document")
                if fn:
                    prof = profile_from_catalog_filename(str(fn))
                    if prof:
                        return prof
    for fname, entry in load_brochure_catalog().items():
        if not isinstance(entry, dict):
            continue
        stem = Path(fname).stem
        if did == fname or did == stem or did in fname or stem in did:
            return labels_for(sanitize_brochure_payload(
                {**entry, "filled_by": "catalog"}, fname))
    return None


def _display_head(insurer: str, name: str) -> str:
    """보험사명이 상품명에 이미 있으면 한 번만."""
    name = (name or "").strip() or "이 상품"
    insurer = (insurer or "").strip()
    if not insurer:
        return name
    if insurer in name or name.startswith(insurer):
        return name
    # 「메리츠」가 이름에 있고 보험사가 「메리츠화재」인 경우 등
    short = insurer.replace("손해보험", "").replace("생명", "").replace(
        "화재", "").replace("보험", "").strip()
    if short and len(short) >= 2 and short in name:
        return name
    return f"{insurer} {name}".strip()


def _summary_core(summary: str) -> str:
    """‘…상품요약서입니다’ 메타 문장 건너뛰고 실질 안내만."""
    text = (summary or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in text.replace("다. ", "다.|").split("|") if p.strip()]
    # 단순 분리: 마침표 기준
    sents = [s.strip() for s in re.split(r"(?<=다)\.\s*", text) if s.strip()]
    if not sents:
        sents = [text]
    picked: list[str] = []
    for s in sents:
        chunk = s if s.endswith("다") or s.endswith("요") else s
        if _META_SUMMARY_RE.search(chunk) or chunk.endswith("상품요약서입니다"):
            continue
        if "상품요약서입니다" in chunk and len(chunk) < 80:
            continue
        picked.append(chunk if chunk.endswith(("다", "요", "음")) else chunk)
        if len(picked) >= 2:
            break
    if not picked:
        # 전부 메타면 두 번째 문장 이후라도
        for s in sents[1:]:
            if s:
                picked.append(s)
                break
    out = ". ".join(picked).strip()
    if out and not out.endswith(("다", "요", ".", "다.")):
        out = out.rstrip(".") + "."
    elif out and out.endswith("다"):
        out = out + "."
    return out[:320]


def _nature_short(nature_label: str, cost_nature: str) -> str:
    # 혼합 라벨("혼합(정액+실손 특약)")에는 '정액'·'실손'이 모두 들어 있으므로
    # 혼합을 먼저 판정해야 KB 뉴라이프 같은 혼합형이 '정액형'으로 새지 않는다.
    lab = nature_label or ""
    if cost_nature == "mixed" or "혼합" in lab:
        return "혼합형(정액+실손 특약)"
    if cost_nature == "lump_sum" or "정액" in lab:
        return "정액형"
    if cost_nature == "indemnity" or "실손" in lab:
        return "실손형"
    return lab.strip()


def build_suggest_facts(profile: dict, kind: str) -> dict:
    """LLM·fallback 공용 facts (환각 방지용 슬롯)."""
    p = profile or {}
    name = str(p.get("display_name") or "").strip() or "이 상품"
    insurer = str(p.get("insurer") or "").strip()
    line = str(p.get("product_line_label") or "").strip() or (
        PRODUCT_LINES.get(str(p.get("product_line") or ""), "") or "보험"
    )
    cost = str(p.get("cost_nature") or "").strip()
    nature_lab = str(p.get("cost_nature_label") or "").strip() or (
        COST_NATURES.get(cost, "") or ""
    )
    summary = str(p.get("summary") or "").strip()
    codes = list(p.get("coverage_codes") or [])
    axes = [_AXIS_LABELS[c] for c in codes if c in _AXIS_LABELS]
    flags = {
        "renewal": "비갱신" not in summary and "갱신" in summary,
        "non_renewal": "비갱신" in summary,
        "surrender_none": (
            "해약환급금 미지급" in summary or "환급금이 없" in summary
        ),
        "surrender_low": "저해약" in summary or (
            "해약환급" in summary and "미지급" not in summary
        ),
        "simple_uw": any(
            k in summary for k in ("간편고지", "간편심사", "간편가입")
        ),
        "waiting": "면책" in summary or "감액" in summary,
    }
    return {
        "kind": kind,
        "head": _display_head(insurer, name),
        "insurer": insurer,
        "display_name": name,
        "product_line": line,
        "cost_nature": cost,
        "nature_label": nature_lab,
        "nature_short": _nature_short(nature_lab, cost),
        "summary_core": _summary_core(summary),
        "summary": summary[:500],
        "axes": axes,
        "flags": flags,
    }


def _topic_phrase(facts: dict) -> str:
    line = facts.get("product_line") or "보험"
    ns = facts.get("nature_short") or ""
    if ns and line:
        if "건강" in line:
            return f"{ns} {line}"
        return f"{line}({ns})" if ns else line
    return line or ns or "보험"


def _particle_eun_neun(word: str) -> str:
    """받침 있으면 '은', 없으면 '는'. 은(는) 표기 금지."""
    w = (word or "").strip()
    if not w:
        return "는"
    ch = w[-1]
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def answer_from_catalog(profile: dict, kind: str) -> str:
    """개선된 결정론 fallback. 은(는)·메타 인용 금지."""
    facts = build_suggest_facts(profile, kind)
    head = facts["head"]
    topic = _topic_phrase(facts)
    core = facts.get("summary_core") or ""
    axes = facts.get("axes") or []
    flags = facts.get("flags") or {}

    if kind == KIND_PRODUCT_TYPE:
        body = (
            f"{head}{_particle_eun_neun(head)} {topic}으로 "
            "안내되는 상품입니다."
        )
        if "간편" in (facts.get("summary") or "") or flags.get("simple_uw"):
            body += " 간편심사·간편고지형으로 안내된 부분이 있습니다."
        if core:
            body += f" {core}"
        if facts.get("nature_short") == "정액형":
            body += (
                " 실제 의료비를 비례 보상하는 실손과는 달리, "
                "약정 금액을 지급하는 정액 구조로 이해하면 됩니다."
            )
        elif facts.get("nature_short", "").startswith("실손"):
            body += " 실제 부담 의료비를 보전하는 실손 성격으로 안내됩니다."
        elif "혼합" in (facts.get("nature_short") or ""):
            body += (
                " 정액 특약과 실손 특약을 한 상품에서 함께 볼 수 있는 "
                "혼합형으로 안내됩니다."
            )
        body += " 자세한 조건은 상품요약서 PDF·약관을 확인해 주세요."
        return _sanitize_answer(body)

    if kind == KIND_COVERAGES:
        if axes:
            body = (
                f"{head} 요약서에서 안내하는 보장 축은 "
                f"{'·'.join(axes)} 등입니다."
            )
        else:
            body = f"{head} 요약서의 주요 보장 축은 짧게만 적혀 있습니다."
        if core:
            body += f" {core}"
        body += " 실제 가입 담보는 설계·특약 선택에 따라 달라질 수 있습니다."
        return _sanitize_answer(body)

    notes: list[str] = []
    if flags.get("non_renewal"):
        notes.append("비갱신형으로 안내된 부분이 있습니다")
    elif flags.get("renewal"):
        notes.append(
            "갱신형 담보·구조가 안내되어 갱신 때 보험료가 달라질 수 있습니다"
        )
    if flags.get("surrender_none"):
        notes.append(
            "해약환급금 미지급형이면 중도 해지 시 환급이 없을 수 있습니다"
        )
    elif flags.get("surrender_low"):
        notes.append("해약환급·저해약환급 관련 안내가 요약서에 있습니다")
    if flags.get("simple_uw"):
        notes.append(
            "간편고지·간편심사 유형이 있어 고지 문항·보험료가 달라질 수 있습니다"
        )
    if flags.get("waiting"):
        notes.append("면책·감액 기간이 있을 수 있습니다")
    if notes:
        body = (
            f"{head} 요약서 기준으로 알아둘 점은 다음과 같습니다. "
            + "; ".join(notes) + "."
        )
    else:
        body = (
            f"{head} 요약서에는 갱신·해약환급·고지 안내가 짧게만 있습니다. "
            "해당 조항은 상품요약서 PDF와 약관에서 확인해 주세요."
        )
    body += " 확정 수치는 지어내지 않으며, 가입 전 상담·약관이 우선입니다."
    return _sanitize_answer(body)


def _sanitize_answer(text: str) -> str:
    out = _DOC_MARKER_RE.sub("", text or "")
    out = _BAD_PARTICLE_RE.sub("는", out)
    out = _WS_RE.sub(" ", out).strip()
    out = re.sub(r"\s+\.", ".", out)
    # 이중 보험사 표기 완화
    out = out.replace("메리츠화재 메리츠 ", "메리츠 ")
    return out


def polish_suggest_answer(
        facts: dict,
        question: str,
        llm,
) -> str | None:
    """facts만으로 쉬운 존댓말 윤문. 실패 시 None."""
    if llm is None or not hasattr(llm, "complete"):
        return None
    prompt = (
        "당신은 보험 상품요약서 안내 도우미입니다. "
        "아래 JSON facts만 사용해 질문에 답하세요. "
        "쉬운 존댓말 2~4문장. "
        "가입 권유·월 보험료 확정·[문서 N]·은(는)/이(가) 표기 금지. "
        "『상품요약서입니다』만 인용하지 마세요. "
        "종류 질문이면 건강보험/암/종신 + 실손·정액·혼합을 분명히 쓰세요. "
        "facts에 없는 특약·일수·금액을 만들지 마세요. "
        "문장만 출력하고 JSON·마크다운 헤딩은 쓰지 마세요."
    )
    ctx = json.dumps(
        {"question": question, "facts": facts},
        ensure_ascii=False,
    )
    try:
        answer, _ = llm.complete(prompt, [ctx], ["suggest-1"])
    except Exception as e:  # noqa: BLE001
        logger.info("suggest polish skip: %s", type(e).__name__)
        return None
    text = _sanitize_answer(str(answer or ""))
    if len(text) < 20:
        return None
    if _SOLICIT_RE.search(text):
        return None
    if "찾지 못했습니다" in text or "근거가 없" in text:
        return None
    return text


def compose_suggest_answer(
        profile: dict,
        kind: str,
        question: str,
        llm=None,
) -> str:
    """LLM 윤문 우선, 실패 시 개선 fallback. 항상 비지 않음."""
    facts = build_suggest_facts(profile, kind)
    polished = polish_suggest_answer(facts, question, llm)
    if polished:
        return polished
    return answer_from_catalog(profile, kind)


def try_brochure_suggest_answer(
        query: str,
        document_ids: list[str] | None,
        metadata=None,
        llm=None,
) -> tuple[str, str] | None:
    """(answer, document_id) 또는 None."""
    matched = match_suggest_question(query)
    if matched is None:
        return None
    docs = [d for d in (document_ids or []) if str(d).strip()]
    if not docs:
        return None
    did = str(docs[0]).strip()
    profile = resolve_profile_for_document(did, metadata)
    if profile is None:
        logger.info("brochure suggest profile miss did=%s", did[:16])
        profile = {
            "display_name": "선택하신 상품요약서",
            "product_line_label": "보험",
            "cost_nature_label": "",
            "cost_nature": "",
            "summary": "",
            "coverage_codes": [],
            "insurer": "",
        }
    answer = compose_suggest_answer(
        profile, matched.kind, matched.question, llm=llm)
    return answer, did


def all_catalog_profiles() -> list[tuple[str, dict]]:
    """테스트용: (filename, labels) 목록."""
    out: list[tuple[str, dict]] = []
    for fname, entry in load_brochure_catalog().items():
        if not isinstance(entry, dict):
            continue
        out.append((fname, labels_for(sanitize_brochure_payload(
            {**entry, "filled_by": "catalog"}, fname))))
    return out
