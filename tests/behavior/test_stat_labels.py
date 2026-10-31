"""흔함·심각도 라벨 경계."""
from harag.guidance.stat_labels import (
    MISSING,
    commonness_from_counts,
    severity_label,
)


def test_severity_bands():
    assert severity_label(None)["missing"] is True
    assert severity_label(None)["label"] == MISSING
    assert severity_label(0.05)["label"] == "대부분 외래 중심"
    assert severity_label(0.08)["label"] == "대부분 외래 중심"
    assert severity_label(0.11)["label"] == "외래 위주이며 입원 비중은 낮은 편"
    assert severity_label(0.15)["label"] == "외래 위주이며 입원 비중은 낮은 편"
    assert severity_label(0.2)["label"] == "입원 비중을 함께 볼 필요"
    assert severity_label(0.2)["avg_visit_days"] is None


def test_commonness_needs_two_counts():
    one = commonness_from_counts([("R25", 100)])
    assert one["missing"] is True
    assert one["label"] == MISSING


def test_commonness_percentile_bands():
    # 5개: rank1 pct=0 → 흔한, rank2 pct=0.25 → 보통, rank5 pct=1 → 드문
    counts = [
        ("A", 500),
        ("B", 400),
        ("C", 300),
        ("D", 200),
        ("E", 100),
    ]
    top = commonness_from_counts(counts, focus_kcd="A")
    assert top["missing"] is False
    assert top["label"] == "흔한 편"
    assert top["rank"] == 1
    mid = commonness_from_counts(counts, focus_kcd="B")
    assert mid["label"] == "보통"
    rare = commonness_from_counts(counts, focus_kcd="E")
    assert rare["label"] == "드문 편"
    assert rare["rank"] == 5
