from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from knowledge_capture.interests import infer_interests


NOW = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)


def record(n, **changes):
    return {"source_id": f"source-{n}", "version_id": f"version-{n}",
            "content_hash": f"hash-{n}", "title": f"资料{n}",
            "url": f"https://example.org/{n}", "origin": "user",
            "collected_at": NOW.isoformat(),
            "topics": [{"name": "RouterOS 安全", "reason": "文章介绍安全更新",
                        "evidence": [{"start_line": 1, "end_line": 2, "quote": "版本更新修复漏洞"}]}],
            **changes}


def test_three_independent_user_sources_make_candidate_without_enabling_discovery():
    result = infer_interests([record(1), record(2), record(3)], now=NOW)[0]
    assert result["state"] == "candidate"
    assert result["user_source_count"] == 3
    assert result["eligible_for_discovery"] is False
    assert result["evidence"][0]["version_id"]
    assert result["evidence"][0]["evidence"][0]["quote"] == "版本更新修复漏洞"


def test_discovery_never_reinforces_user_interest():
    data = [record(1)] + [record(i, origin="discovery") for i in range(2, 20)]
    result = infer_interests(data, now=NOW)[0]
    assert result["state"] == "watching"
    assert result["user_source_count"] == 1
    assert infer_interests(data[1:], now=NOW) == []


def test_duplicates_versions_and_mirrors_do_not_reach_threshold():
    data = [record(1), record(1), record(2, content_hash="hash-1"),
            record(1, version_id="new", content_hash="new-hash",
                   collected_at=(NOW-timedelta(days=1)).isoformat())]
    assert infer_interests(data, now=NOW)[0]["user_source_count"] == 1


def test_latest_source_version_wins_even_when_old_version_has_other_topic():
    old = record(1, collected_at=(NOW-timedelta(days=1)).isoformat())
    latest = record(1, version_id="latest", topics=[])
    assert infer_interests([old, latest], now=NOW) == []


def test_window_excludes_future_and_expired_includes_boundary():
    data = [record(1, collected_at=(NOW-timedelta(days=30)).isoformat()),
            record(2, collected_at=(NOW-timedelta(days=30, seconds=1)).isoformat()),
            record(3, collected_at=(NOW+timedelta(seconds=1)).isoformat()),
            record(4, collected_at="2026-10-06T08:00:00"),
            record(5, collected_at="invalid")]
    result = infer_interests(data, now=NOW)[0]
    assert result["user_source_count"] == 1
    assert result["evidence"][0]["source_id"] == "source-1"


@pytest.mark.parametrize("state,eligible", [("followed", True), ("paused", False), ("closed", False)])
def test_explicit_feedback_controls_state_and_discovery(state, eligible):
    data = [record(1)]
    key = infer_interests(data, now=NOW)[0]["id"]
    result = infer_interests(data, now=NOW, feedback={key: state})[0]
    assert result["state"] == state
    assert result["eligible_for_discovery"] is eligible


def test_unicode_whitespace_casefold_ids_stable_but_no_semantic_merge():
    a, b, c = record(1), record(2), record(3)
    b["topics"][0]["name"] = "  ＲＯＵＴＥＲＯＳ\t安全  "
    c["topics"][0]["name"] = "路由器系统安全"
    results = infer_interests([a, b, c], now=NOW)
    assert sorted(x["user_source_count"] for x in results) == [1, 2]
    assert infer_interests([a], now=NOW)[0]["id"] == infer_interests([b], now=NOW)[0]["id"]
    assert results == infer_interests([c, b, a], now=NOW)


def test_duplicate_topics_only_count_once_and_evidence_is_detached():
    a = record(1)
    a["topics"] *= 2
    before = deepcopy(a)
    result = infer_interests([a], now=NOW)[0]
    assert result["user_source_count"] == 1
    result["evidence"][0]["evidence"][0]["quote"] = "changed"
    assert a == before


@pytest.mark.parametrize("overrides", [
    {"origin": "unknown"}, {"content_hash": ""}, {"source_id": None},
    {"topics": []}, {"topics": None}, {"topics": [{"name": "x"}]},
    {"topics": [{"name": "x", "reason": "why", "evidence": [{"start_line": 0, "end_line": 1, "quote": "x"}]}]},
])
def test_malformed_records_cannot_create_interest(overrides):
    assert infer_interests([None, record(1, **overrides)], now=NOW) == []


@pytest.mark.parametrize("kwargs", [
    {"window_days": 0}, {"threshold": True}, {"threshold": 1.5},
    {"now": datetime(2026, 10, 6)}, {"feedback": {"x": "candidate"}},
    {"feedback": {"x": []}},
])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        infer_interests([], **kwargs)
