from unittest.mock import Mock

import pytest

from knowledge_capture.capture import CaptureError
from knowledge_capture.discovery import Discovery, DiscoveryError
from knowledge_capture.processing import Processor
from knowledge_capture.search import SearchError
from knowledge_capture.store import Store
from knowledge_capture.wiki import Wiki


def capture_text(text):
    def capture(url, assets):
        return {"title": "设备知识", "markdown": text, "original_url": url, "final_url": url,
                "author": None, "published_at": None, "assets": [], "warnings": [], "status": "complete"}
    return capture


class TestModel:
    __test__ = False
    identity = {"provider": "test-fixture", "model": "fixed-not-real-ai"}
    def __init__(self, relevant=True, novel=True):
        self.relevant, self.novel = relevant, novel
        self.calls = []
    def complete_json(self, system, payload):
        self.calls.append(payload)
        if "sources" in payload:
            source = payload["sources"][0]
            line = source["lines"][0]
            return {"summary": [{"text": "固定Wiki测试", "evidence": [{"source_id": source["source_id"],
                     "version_id": source["version_id"], "start_line": line["number"], "end_line": line["number"]}]}],
                    "agreements": [], "differences": [], "questions": []}
        lines = payload.get("candidate", payload)["lines"]
        line = lines[0]
        cite = {"start_line": line["number"], "end_line": line["number"], "quote": line["text"]}
        if "candidate" in payload:
            return {"relevant": self.relevant, "novel": self.novel, "reason": "固定筛选理由", "evidence": [cite]}
        topic = "路由器维护" if "初始" in line["text"] else "路由器维护的相关主题"
        return {"summary": [{"text": "固定摘要", "evidence": [cite]}], "key_points": [],
                "topics": [{"name": topic, "reason": "固定主题", "evidence": [cite]}], "questions": []}


@pytest.fixture
def followed(tmp_path):
    store = Store(tmp_path)
    item = store.ingest("https://example.org/initial", capture_fn=capture_text("初始资料说明设备版本信息必须核实。"))
    processor = Processor(store)
    processor.analyze(item["source_id"], TestModel())
    topic = processor.interests()[0]
    processor.feedback(topic["id"], "followed")
    return store, topic["id"]


def search_result(url="https://example.org/new"):
    return Mock(search=Mock(return_value=[{"url": url, "title": "新资料", "description": "仅搜索片段"}]))


def test_new_content_is_analyzed_and_wiki_updates_without_reinforcing_interest(followed):
    store, topic_id = followed
    result = Discovery(store).run(topic_id, search_client=search_result(), model_client=TestModel(),
                                  capture_fn=capture_text("新增资料介绍新的部署条件和设备维护方法。"))
    assert result["status"] == "complete"
    assert result["candidates"][0]["status"] == "added"
    assert result["wiki"]["status"] == "complete"
    assert len(Wiki(store).read(topic_id)["record"]["dependencies"]) == 2
    topic = next(t for t in Processor(store).interests() if t["id"] == topic_id)
    assert topic["user_source_count"] == 1
    assert any(c["origin"] == "discovery" for c in store.captures())


def test_duplicates_do_not_call_model_or_add_source(followed):
    store, topic_id = followed
    model = TestModel()
    result = Discovery(store).run(topic_id, search_client=search_result(), model_client=model,
                                  capture_fn=capture_text("初始资料说明设备版本信息必须核实。"))
    assert result["status"] == "no_new"
    assert result["candidates"][0]["status"] == "duplicate"
    assert model.calls == [] and len(store.list_sources()) == 1


def test_linked_discovery_with_different_topic_name_is_compared_next_time(followed):
    store, topic_id = followed
    discovery = Discovery(store)
    first = discovery.run(topic_id, search_client=search_result(), model_client=TestModel(),
                          capture_fn=capture_text("新增资料介绍新的部署条件和设备维护方法。"), refresh_wiki=False)
    added_id = first['candidates'][0]['source_id']
    model = TestModel(novel=False)
    discovery.run(topic_id, search_client=search_result('https://example.org/another'), model_client=model,
                  capture_fn=capture_text("另一篇资料重新讨论已经收录过的设备维护方法。"), refresh_wiki=False)
    assert added_id in {item['source_id'] for item in model.calls[0]['existing']}


def test_irrelevant_results_preserve_rejection_evidence(followed):
    store, topic_id = followed
    result = Discovery(store).run(topic_id, search_client=search_result(), model_client=TestModel(relevant=False),
                                  capture_fn=capture_text("这篇资料实际是在讨论完全不同的主题。"))
    assert result["status"] == "no_new"
    assert result["candidates"][0]["status"] == "rejected"
    assert "start_line" in result["candidates"][0]["reason"]
    assert len(store.list_sources()) == 1


def test_capture_error_is_not_no_new(followed):
    store, topic_id = followed
    result = Discovery(store).run(topic_id, search_client=search_result(), model_client=TestModel(),
                                  capture_fn=Mock(side_effect=CaptureError("no_content", "需要访问验证")))
    assert result["status"] == "partial"
    assert result["candidates"][0]["error_code"] == "no_content"


def test_empty_results_and_failed_search_are_distinct(followed):
    store, topic_id = followed
    discovery = Discovery(store)
    result = discovery.run(topic_id, search_client=Mock(search=Mock(return_value=[])), model_client=TestModel())
    assert result["status"] == "no_results"
    with pytest.raises(DiscoveryError) as exc:
        discovery.run(topic_id, search_client=Mock(search=Mock(side_effect=SearchError("rate_limited", "搜索限流"))), model_client=TestModel())
    assert exc.value.code == "rate_limited"
    assert discovery.history()[0]["status"] == "failed"


def test_paused_topic_never_searches(followed):
    store, topic_id = followed
    Processor(store).feedback(topic_id, "paused")
    search = search_result()
    with pytest.raises(DiscoveryError) as exc:
        Discovery(store).run(topic_id, search_client=search, model_client=TestModel())
    assert exc.value.code == "topic_not_followed"
    search.search.assert_not_called()


def test_daily_query_budget_persists_across_instances(followed):
    store, topic_id = followed
    search = Mock(search=Mock(return_value=[]))
    Discovery(store, daily_queries=1).run(topic_id, search_client=search, model_client=TestModel())
    with pytest.raises(DiscoveryError) as exc:
        Discovery(store, daily_queries=1).run(topic_id, search_client=search, model_client=TestModel())
    assert exc.value.code == "budget_exhausted"
    assert search.search.call_count == 1


def test_model_budget_keeps_collected_source_and_reports_unfinished_analysis(followed):
    store, topic_id = followed
    result = Discovery(store, daily_model_calls=1).run(topic_id, search_client=search_result(), model_client=TestModel(),
                                                       capture_fn=capture_text("新增资料介绍不同部署环境中的维护条件。"))
    assert result["status"] == "partial"
    candidate = result["candidates"][0]
    assert candidate["status"] == "analysis_failed" and candidate["source_id"]
    assert candidate["error_code"] == "budget_exhausted"
    assert result["wiki"] is None


def test_daily_addition_limit_not_bypassed_by_new_run(followed):
    store, topic_id = followed
    discovery = Discovery(store, daily_additions_per_topic=1)
    discovery.run(topic_id, search_client=search_result(), model_client=TestModel(), capture_fn=capture_text("第一条新增资料讨论部署约束。"))
    result = discovery.run(topic_id, search_client=search_result("https://example.org/another"), model_client=TestModel(),
                           capture_fn=capture_text("第二条新增资料提出另一种部署条件。"))
    assert result["status"] == "partial"
    assert result["candidates"][0]["error_code"] == "addition_budget_exhausted"
    assert len(store.list_sources()) == 2
