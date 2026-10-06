import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from knowledge_capture.llm import LLMError
from knowledge_capture.processing import AnalysisError, Processor, source_chunks, validate_analysis
from knowledge_capture.store import Store


class EvidenceClient:
    identity = {"provider": "test-fixture", "model": "fixed-response-not-real-ai"}

    def __init__(self):
        self.calls = []

    def complete_json(self, system, payload):
        self.calls.append(payload)
        line = next(line for line in payload["lines"] if len(line["text"]) >= 4)
        citation = {"start_line": line["number"], "end_line": line["number"], "quote": line["text"][:30]}
        return {"summary": [{"text": "固定测试整理内容", "evidence": [citation]}],
                "key_points": [], "topics": [{"name": "设备版本管理", "reason": "固定测试主题", "evidence": [citation]}],
                "questions": ["哪些条件仍需核实？"]}


def capture_fixture(text="设备版本管理需要保留当前版本与更新时间。", image=False, status="complete"):
    def capture(url, directory):
        assets = []
        if image:
            data = b"test-image"
            (directory / "diagram.png").write_bytes(data)
            assets.append({"original_url": "https://example.org/image.png", "relative_path": "assets/diagram.png",
                           "status": "complete", "sha256": hashlib.sha256(data).hexdigest()})
        return {"title": "测试资料", "markdown": text + ("\n![图片](assets/diagram.png)" if image else ""),
                "original_url": url, "final_url": url, "author": None, "published_at": None,
                "status": status, "assets": assets, "warnings": ["有缺失内容"] if status == "partial" else []}
    return capture


def test_analysis_keeps_original_and_packages_citations_and_images(tmp_path):
    store = Store(tmp_path / "data")
    capture = store.ingest("https://example.org/one", capture_fn=capture_fixture(image=True), note="私密收藏备注不发模型")
    original = Path(capture["path"]).read_bytes()
    processor = Processor(store)
    client = EvidenceClient()
    result = processor.analyze(capture["source_id"], client)
    assert Path(capture["path"]).read_bytes() == original
    assert "私密" not in json.dumps(client.calls, ensure_ascii=False)
    read = processor.read(result["id"])
    assert read["record"]["source_version"] == capture["version_id"]
    assert "evidence.md#L1-L1" in read["markdown"]
    assert '<a id="L1-L1"></a>' in (Path(read["path"]) / "evidence.md").read_text()
    output = processor.export(result["id"], tmp_path / "analysis.zip")
    with zipfile.ZipFile(output) as archive:
        assert archive.read(result["id"] + "/source.md") == original
        assert archive.read(result["id"] + "/assets/diagram.png") == b"test-image"
        source_metadata = json.loads(archive.read(result["id"] + "/source_metadata.json"))
        assert source_metadata["assets"][0]["original_url"] == "https://example.org/image.png"


def test_invalid_citation_fails_without_publishing_analysis(tmp_path):
    store = Store(tmp_path)
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture())
    class InvalidClient(EvidenceClient):
        def complete_json(self, system, payload):
            result = super().complete_json(system, payload)
            result["summary"][0]["evidence"][0]["quote"] = "这句话在原文中根本不存在"
            return result
    processor = Processor(store)
    with pytest.raises(AnalysisError, match="摘录"):
        processor.analyze(item["source_id"], InvalidClient())
    assert processor.history()[0]["status"] == "failed"
    assert processor.history()[0]["error_code"] == "invalid_citation"
    assert not list(tmp_path.glob("analyses/**/analysis.md"))
    assert store.list_sources()[0]["latest_version"] == item["version_id"]


def test_timeout_is_visible_no_automatic_model_retry(tmp_path):
    store = Store(tmp_path)
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture())
    processor = Processor(store)
    client = EvidenceClient()
    with patch.object(client, "complete_json", side_effect=LLMError("timeout", "服务超时")) as call:
        with pytest.raises(LLMError):
            processor.analyze(item["source_id"], client)
    assert call.call_count == 1
    assert processor.history()[0]["error_code"] == "timeout"


def test_missing_configuration_records_failure_without_network(tmp_path, monkeypatch):
    for name in ("KC_LLM_BASE_URL", "KC_LLM_MODEL", "KC_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    store = Store(tmp_path)
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture())
    processor = Processor(store)
    with patch("requests.Session") as session, pytest.raises(LLMError):
        processor.analyze(item["source_id"])
    session.assert_not_called()
    assert processor.history()[0]["error_code"] == "configuration_missing"


def test_all_chunks_processed_without_truncation(tmp_path):
    store = Store(tmp_path)
    body = "\n".join("第%d行：版本条件必须核实。" % n for n in range(20))
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture(body))
    client = EvidenceClient()
    with patch("knowledge_capture.processing.source_chunks", side_effect=lambda body: source_chunks(body, max_chars=90)):
        result = Processor(store).analyze(item["source_id"], client)
    numbers = [line["number"] for call in client.calls for line in call["lines"]]
    assert numbers == list(range(1, 21))
    assert result["chunks_processed"] > 1


def test_later_chunk_failure_does_not_publish_partial_analysis(tmp_path):
    store = Store(tmp_path)
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture("版本必须核实。\n升级需要验证。\n数据必须保留。"))
    client = EvidenceClient()
    original = client.complete_json
    def fail_second(system, payload):
        if payload["chunk"] == 2:
            raise LLMError("timeout", "服务超时")
        return original(system, payload)
    with patch("knowledge_capture.processing.source_chunks", side_effect=lambda body: source_chunks(body, max_chars=10)):
        with patch.object(client, "complete_json", side_effect=fail_second), pytest.raises(LLMError):
            Processor(store).analyze(item["source_id"], client)
    assert not list(tmp_path.glob("analyses/**/analysis.md"))


def test_partial_source_stays_partial_after_analysis(tmp_path):
    store = Store(tmp_path)
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture(status="partial"))
    result = Processor(store).analyze(item["source_id"], EvidenceClient())
    assert result["status"] == "partial"


def test_three_user_documents_trigger_candidate_but_discovery_does_not(tmp_path):
    store = Store(tmp_path)
    processor = Processor(store)
    for n in range(5):
        item = store.ingest(f"https://example.org/{n}", origin="user" if n < 3 else "discovery",
                            capture_fn=capture_fixture(f"第{n}篇资料：设备版本信息需要独立核实。"))
        processor.analyze(item["source_id"], EvidenceClient())
    topic, = processor.interests()
    assert topic["user_source_count"] == 3
    assert topic["state"] == "candidate"
    assert topic["eligible_for_discovery"] is False
    assert processor.feedback(topic["id"], "followed")["eligible_for_discovery"] is True
    assert processor.feedback(topic["id"], "paused")["eligible_for_discovery"] is False
    assert processor.feedback(topic["id"], "watching")["state"] == "watching"


def test_feedback_is_preserved_after_evidence_expires(tmp_path):
    store = Store(tmp_path)
    processor = Processor(store)
    item = store.ingest("https://example.org/a", capture_fn=capture_fixture())
    processor.analyze(item["source_id"], EvidenceClient())
    topic, = processor.interests()
    processor.feedback(topic["id"], "closed")
    with store._connect() as db:
        db.execute("UPDATE captures SET created_at='2000-01-01T00:00:00+00:00'")
    topic, = processor.interests()
    assert topic["state"] == "closed" and topic["user_source_count"] == 0


def test_automatically_changed_version_not_counted_as_user_interest(tmp_path):
    store = Store(tmp_path)
    processor = Processor(store)
    old = store.ingest("https://example.org/a", capture_fn=capture_fixture("人工提交：旧主题内容。"))
    processor.analyze(old["source_id"], EvidenceClient())
    assert processor.interests()
    new = store.ingest("https://example.org/a", origin="discovery", capture_fn=capture_fixture("自动获取：页面已经换了内容。"))
    processor.analyze(new["source_id"], EvidenceClient())
    assert processor.interests() == []


def test_source_update_excludes_stale_analysis(tmp_path):
    store = Store(tmp_path)
    processor = Processor(store)
    old = store.ingest("https://example.org/a", capture_fn=capture_fixture())
    processor.analyze(old["source_id"], EvidenceClient())
    store.ingest("https://example.org/a", capture_fn=capture_fixture("另一个全新主题，还没有经过整理。"))
    assert processor.interests() == []


def test_line_limit_raises_instead_of_truncating():
    with pytest.raises(AnalysisError, match="没有截断"):
        source_chunks("a" * 12001)


@pytest.mark.parametrize("start,end,quote", [(0, 1, "真实原文"), (True, 1, "真实原文"), (1, 30, "真实原文"), (1, 1, "假造引文")])
def test_bad_line_references_rejected(start, end, quote):
    result = {"summary": [{"text": "测试", "evidence": [{"start_line": start, "end_line": end, "quote": quote}]}],
              "key_points": [], "topics": [], "questions": []}
    with pytest.raises(AnalysisError):
        validate_analysis(result, [{"number": 1, "text": "真实原文"}])


def test_failed_citation_saves_private_unpublished_diagnostic_without_replacing_success(tmp_path):
    import stat
    from knowledge_capture.processing import SYSTEM_PROMPT, digest, body_of
    store = Store(tmp_path)
    source = store.ingest('https://example.org/diagnose', capture_fn=capture_fixture())
    processor = Processor(store)
    good = processor.analyze(source['source_id'], EvidenceClient())
    good_record = processor.read(good['id'])['record']
    assert good_record['processor'] == 'citation-cleaner/3'
    assert good_record['prompt_hash'] == digest(SYSTEM_PROMPT)
    old_bytes = (Path(processor.read(good['id'])['path']) / 'analysis.json').read_bytes()
    class InvalidCitation(EvidenceClient):
        def complete_json(self, system, payload):
            result = super().complete_json(system, payload)
            result['summary'][0]['evidence'][0]['quote'] = '不在原文中的诊断测试摘录'
            self.returned = copy.deepcopy(result)
            return result
    client = InvalidCitation()
    with pytest.raises(AnalysisError) as exc:
        processor.analyze(source['source_id'], client)
    assert exc.value.code == 'invalid_citation'
    assert len(client.calls) == 1
    failed = next(run for run in processor.history() if run['status'] == 'failed')
    assert failed['path'] is None
    path = tmp_path / '.diagnostics' / 'analyses' / failed['id'] / 'chunk-1.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    diagnostic = json.loads(path.read_text())
    assert diagnostic['status'] == 'unpublished'
    assert diagnostic['run_id'] == failed['id']
    assert diagnostic['source_id'] == source['source_id']
    assert diagnostic['source_version'] == source['version_id']
    assert diagnostic['input_hash'] == digest(body_of(store.read(source['source_id'])['markdown']))
    assert diagnostic['model_json'] == client.returned
    assert diagnostic['input_lines'] == client.calls[0]['lines']
    assert diagnostic['prompt_hash'] == good_record['prompt_hash']
    assert diagnostic['error_code'] == 'invalid_citation'
    assert '不在原文中的诊断测试摘录' not in failed['error']
    with pytest.raises(AnalysisError):
        processor.read(failed['id'])
    assert processor.interest_records()[0]['source_id'] == source['source_id']
    assert (Path(processor.read(good['id'])['path']) / 'analysis.json').read_bytes() == old_bytes


def test_prompt_requires_per_claim_coverage_and_exact_escaped_quotes():
    from knowledge_capture.processing import SYSTEM_PROMPT
    assert '所有分句' in SYSTEM_PROMPT
    assert '不能借用其他条' in SYSTEM_PROMPT
    assert '无条件通则' in SYSTEM_PROMPT
    assert 'Markdown 反斜杠转义' in SYSTEM_PROMPT


def test_twelve_independent_references_fit_bounded_contract():
    from knowledge_capture.processing import MAX_EVIDENCE_PER_ITEM, SYSTEM_PROMPT
    lines = [{'number': i, 'text': f'第{i}项独立事实需要保留其条件。'} for i in range(1, 14)]
    citations = [{'start_line': line['number'], 'end_line': line['number'], 'quote': line['text']} for line in lines]
    payload = {'summary': [{'text': '多个事实各有引用依据。', 'evidence': citations[:12]}],
               'key_points': [], 'topics': [], 'questions': []}
    assert MAX_EVIDENCE_PER_ITEM == 12
    assert '1至12个引用' in SYSTEM_PROMPT
    assert validate_analysis(payload, lines)['summary'][0]['evidence'] == citations[:12]
    payload['summary'][0]['evidence'] = citations
    with pytest.raises(AnalysisError, match='1至12') as exc:
        validate_analysis(payload, lines)
    assert exc.value.code == 'invalid_citation'
    payload['summary'][0]['evidence'] = copy.deepcopy(citations[:12])
    payload['summary'][0]['evidence'][11]['quote'] = '即使引用数量符合约定也不得伪造摘录'
    with pytest.raises(AnalysisError, match='摘录与来源不一致') as exc:
        validate_analysis(payload, lines)
    assert exc.value.code == 'invalid_citation'
