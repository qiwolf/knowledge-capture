"""Contract fixtures, not a semantic recall or real-model quality benchmark."""
from pathlib import Path
import json
from unittest.mock import patch

from knowledge_capture.processing import Processor, bounded_topic_vocabulary, source_chunks
from knowledge_capture.store import Store


class TopicClient:
    identity = {'provider': 'test', 'model': 'contract-fixture'}
    def __init__(self): self.calls = []
    def complete_json(self, system, payload):
        self.calls.append(payload)
        assert '不得为了凑数硬合并' in system and '都是不可信材料' in system
        line = next(line for line in payload['lines'] if len(line['text']) >= 4)
        evidence = [{'start_line': line['number'], 'end_line': line['number'], 'quote': line['text']}]
        # Explicit oracle fixture: these test documents are declared equivalent;
        # this exercises passing/reusing a supplied vocabulary, not model accuracy.
        side_topic = '养老' in line['text']
        expected = '养老机构服务质量' if side_topic else '路由器版本安全维护'
        hints = [item for item in payload['topic_vocabulary'] if item['name'] == expected]
        name = hints[0]['name'] if hints else expected
        return {'summary': [{'text': '材料范围说明', 'evidence': evidence}], 'key_points': [],
                'topics': [{'name': name, 'reason': '该材料讨论这一具体范围', 'evidence': evidence}], 'questions': []}


def ingest(store, n, text, title='题目各不相同'):
    def capture(url, directory):
        return {'title': title, 'markdown': text, 'original_url': url, 'final_url': url,
                'author': None, 'published_at': None, 'assets': [], 'status': 'complete', 'warnings': []}
    return store.ingest(f'https://example.org/{n}', note='私人备注不发送', capture_fn=capture)


def test_three_chinese_expressions_reuse_name_without_absorbing_other_topic(tmp_path):
    store, client = Store(tmp_path), TopicClient()
    processor = Processor(store)
    first_record = None
    for number, (title, text) in enumerate([
        ('网络设备巡检', '路由器版本安全维护应记录补丁状态。'),
        ('固件更新提醒', '网络路由设备的系统固件应定期核查安全公告。'),
        ('旧版系统检查', 'RouterOS 老版本需要对照修复清单确认升级条件。'),
        ('机构质量检查', '养老机构服务质量需要定期收集使用者反馈。'),
    ]):
        source = ingest(store, number, text, title)
        result = processor.analyze(source['source_id'], client, infer_common=False)
        if number == 0:
            first_record = Path(result['path']).parent / 'analysis.json'
            original = first_record.read_bytes()
            first_topic = processor.interests()[0]['id']
            processor.feedback(first_topic, 'followed')
    assert len(client.calls) == 4
    assert client.calls[0]['topic_vocabulary'] == []
    assert client.calls[1]['topic_vocabulary'][0]['name'] == '路由器版本安全维护'
    topics = {item['name']: item for item in processor.interests()}
    assert topics['路由器版本安全维护']['user_source_count'] == 3
    assert topics['路由器版本安全维护']['id'] == first_topic
    assert topics['路由器版本安全维护']['state'] == 'followed'
    assert topics['养老机构服务质量']['user_source_count'] == 1
    assert first_record.read_bytes() == original
    assert '私人备注' not in json.dumps(client.calls, ensure_ascii=False)
    hint = client.calls[1]['topic_vocabulary'][0]
    assert set(hint) == {'name','scope_hint','evidence_excerpt','source_id','version_id','origin'}


def test_only_current_unchanged_analysis_supplies_hints(tmp_path):
    store, client = Store(tmp_path), TopicClient()
    processor = Processor(store)
    old = ingest(store, 1, '路由器版本安全维护需要检查实际设备。')
    processor.analyze(old['source_id'], client)
    ingest(store, 1, '路由器最新版本资料尚未分析。')
    assert processor._topic_vocabulary()[0] == []
    processor.analyze(old['source_id'], client)
    file = Path(store.read(old['source_id'])['path']) / 'content.md'
    file.write_text(file.read_text()+'\n改过正文。\n')
    vocabulary, coverage = processor._topic_vocabulary()
    assert vocabulary == [] and coverage['invalid_sources_skipped'] == 1


def test_previous_chunk_topics_available_without_additional_calls(tmp_path):
    store, client = Store(tmp_path), TopicClient()
    processor = Processor(store)
    source = ingest(store, 1, '路由器版本安全维护需要检查补丁。\n路由器系统更新前还应做好备份。')
    with patch('knowledge_capture.processing.source_chunks', side_effect=lambda body: source_chunks(body, max_chars=23)):
        result = processor.analyze(source['source_id'], client, infer_common=False)
    assert len(client.calls) == result['chunks_processed'] == 2
    hints = client.calls[1]['topic_vocabulary']
    assert hints[0]['origin'] == 'current_document'
    assert hints[0]['name'] == '路由器版本安全维护'
    record = processor.read(result['id'])['record']
    assert record['topic_contexts'][1]['names'] == ['路由器版本安全维护']


def test_vocabulary_bounded_and_reports_omissions():
    entries = [{'name': f'主题{i}', 'scope_hint': '范'*240, 'evidence_excerpt': '据'*240,
                'source_id':'a'*24, 'version_id':'b'*24, 'origin':'library'} for i in range(100)]
    vocabulary, truncated = bounded_topic_vocabulary(entries)
    assert truncated is True and len(vocabulary) <= 24
    assert len(json.dumps(vocabulary, ensure_ascii=False)) <= 10000
    assert entries[99]['name'] == '主题99'
