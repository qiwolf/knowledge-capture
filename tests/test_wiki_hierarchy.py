from copy import deepcopy
import json
from pathlib import Path

import pytest

import knowledge_capture.wiki as module
from knowledge_capture.store import Store
from knowledge_capture.wiki import Wiki, WikiError
from knowledge_capture.processing import digest
from test_wiki import capture


class LayerModel:
    identity = {'provider': 'fixture', 'model': 'hierarchy'}
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def complete_json(self, prompt, payload):
        self.calls.append(payload)
        if payload.get('stage') == 'reduce':
            citations = [item['evidence'][0] for node in payload['partials'] for item in node['result']['summary']]
            # Keep tail evidence, plus one per source, without synthesizing IDs.
            chosen = {}
            for cite in citations:
                chosen[cite['source_id']] = cite
            result = {'summary': [{'text': cite['quote'], 'evidence': [cite]} for cite in chosen.values()],
                      'agreements': [], 'differences': [], 'questions': []}
            if len(chosen) >= 2:
                result['differences'] = [{'text': '不同来源各有适用条件，不能混为一谈。', 'evidence': list(chosen.values())}]
        else:
            source = payload['sources'][0]
            line = next(line for line in reversed(source['lines']) if len(line['text']) >= 4)
            cite = {'source_id': source['source_id'], 'version_id': source['version_id'], 'start_line': line['number'], 'end_line': line['number'], 'quote': line['text']}
            result = {'summary': [{'text': line['text'], 'evidence': [cite]}], 'agreements': [], 'differences': [], 'questions': []}
        if self.change:
            self.change(result, payload)
        return deepcopy(result)


def fixture(tmp_path, monkeypatch, count=2):
    store = Store(tmp_path)
    documents = {}
    for n in range(count):
        text = '\n'.join(f'来源{n}第{i}行说明前置配置条件及需要保留的独立限制。' for i in range(100)) + f'\n来源{n}末块独有新事实：升级后需要重新核对兼容性。'
        item = store.ingest(f'https://example.org/{n}', capture_fn=capture(text))
        documents[item['source_id']] = store.read(item['source_id'])
    wiki = Wiki(store)
    topic = {'id': 'interest_' + 'a' * 24, 'name': '系统更新'}
    deps = {sid: {'version_id': doc['metadata']['version_id'], 'hash': digest(doc['markdown'])} for sid, doc in documents.items()}
    monkeypatch.setattr(wiki, '_inputs', lambda _: (topic, documents, deps))
    monkeypatch.setattr(module, 'MAX_INPUT_CHARS', 3000)
    monkeypatch.setattr(module, 'MAX_CHUNK_CHARS', 1600)
    return wiki, topic, documents


def test_multichunk_all_lines_tail_cross_source_and_snapshots(tmp_path, monkeypatch):
    wiki, topic, documents = fixture(tmp_path, monkeypatch)
    model = LayerModel()
    output = wiki.build(topic['id'], model, protocol="quote-v1")
    page = wiki.read(topic['id'])
    record = page['record']
    assert record['coverage']['strategy'] == 'hierarchical'
    assert record['coverage']['model_calls'] == len(model.calls)
    assert record['coverage']['levels'] >= 2
    assert record['coverage']['all_source_lines_processed']
    for sid, doc in documents.items():
        read = [line['number'] for payload in model.calls if payload.get('stage') == 'extract' for source in payload['sources'] if source['source_id'] == sid for line in source['lines']]
        assert read == list(range(1, len(module.body_of(doc['markdown']).splitlines()) + 1))
        saved = wiki.store.root / 'wiki/versions' / topic['id'] / output['version_id'] / 'sources' / sid / 'source.md'
        assert saved.read_text() == doc['markdown']
    assert '末块独有新事实' in page['markdown']
    assert len(record['result']['differences'][0]['evidence']) == 2
    assert all(len(json.dumps(p, ensure_ascii=False)) <= 3000 for p in model.calls)


def test_leaf_cannot_cite_unseen_lines(tmp_path, monkeypatch):
    wiki, topic, documents = fixture(tmp_path, monkeypatch)
    def change(result, payload):
        if payload.get('stage') == 'extract':
            sid = payload['sources'][0]['source_id']
            lines = module.body_of(documents[sid]['markdown']).splitlines()
            result['summary'][0]['evidence'][0].update(start_line=len(lines), end_line=len(lines), quote=lines[-1])
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], LayerModel(change), protocol="quote-v1")
    assert exc.value.code == 'citation_outside_input'
    assert not list((tmp_path / '.cache/wiki/leaves').glob('*.json'))
    assert wiki.list_pages() == []


def test_reduce_cannot_introduce_other_valid_original_quote(tmp_path, monkeypatch):
    wiki, topic, documents = fixture(tmp_path, monkeypatch)
    def change(result, payload):
        if payload.get('stage') == 'reduce':
            cite = result['summary'][0]['evidence'][0]
            cite.update(start_line=1, end_line=1, quote=module.body_of(documents[cite['source_id']]['markdown']).splitlines()[0])
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], LayerModel(change), protocol="quote-v1")
    assert exc.value.code == 'citation_outside_input'
    assert wiki.list_pages() == []


def test_last_leaf_failure_never_publishes(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    def change(result, payload):
        if payload.get('stage') == 'extract' and payload['sources'][0]['lines'][-1]['number'] == 101:
            result['summary'][0]['evidence'][0]['quote'] = '完全不存在的伪造摘录'
    with pytest.raises(WikiError):
        wiki.build(topic['id'], LayerModel(change), protocol="quote-v1")
    assert wiki.list_pages() == []
    assert not (wiki.store.root / 'wiki/versions').exists()


def test_long_line_or_chunk_limit_rejected_before_calls(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    model = LayerModel()
    monkeypatch.setattr(module, 'MAX_LINE_CHARS', 10)
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], model, protocol="quote-v1")
    assert exc.value.code == 'line_too_large' and not model.calls
    monkeypatch.setattr(module, 'MAX_LINE_CHARS', 24000)
    monkeypatch.setattr(module, 'MAX_LEAF_CHUNKS', 1)
    with pytest.raises(WikiError):
        wiki.build(topic['id'], model, protocol="quote-v1")
    assert not model.calls


def test_empty_unrelated_chunks_no_reduce_and_no_invented_fact(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    def empty(result, payload):
        for key in result:
            result[key] = []
    model = LayerModel(empty)
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], model, protocol="quote-v1")
    assert exc.value.code == 'no_topic_evidence'
    assert all(p['stage'] == 'extract' for p in model.calls)
    assert wiki.list_pages() == []


def test_small_payload_keeps_one_original_call(tmp_path):
    from test_wiki import Analyzer, Synthesizer
    from knowledge_capture.processing import Processor
    store = Store(tmp_path)
    source = store.ingest('https://example.org/small', capture_fn=capture('升级前应备份设备配置。'))
    processor = Processor(store)
    processor.analyze(source['source_id'], Analyzer())
    topic = processor.interests()[0]['id']
    model = LayerModel()
    Wiki(store).build(topic, model, protocol="quote-v1")
    assert len(model.calls) == 1 and 'stage' not in model.calls[0]
    assert Wiki(store).read(topic)['record']['coverage']['strategy'] == 'single'


def test_budget_failure_charges_each_call_and_keeps_previous_page(tmp_path, monkeypatch):
    from knowledge_capture.llm import LLMError
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    wiki.build(topic['id'], LayerModel(), protocol="quote-v1")
    original = wiki.read(topic['id'])
    class Budget:
        identity = {**LayerModel.identity, 'model': 'fresh-budget-test'}
        def __init__(self):
            self.used = 0
            self.inner = LayerModel()
        def complete_json(self, system, payload):
            if self.used >= 2:
                raise LLMError('budget_exhausted', '测试预算耗尽')
            self.used += 1
            return self.inner.complete_json(system, payload)
    client = Budget()
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], client, protocol="quote-v1")
    assert exc.value.code == 'budget_exhausted'
    assert client.used == len(client.inner.calls) == 2
    assert wiki.read(topic['id'])['version_id'] == original['version_id']
    assert wiki.read(topic['id'])['markdown'] == original['markdown']


def test_hierarchical_snapshot_keeps_local_image(tmp_path, monkeypatch):
    from workbench_ui_fixture import png_fixture
    import hashlib
    wiki, topic, documents = fixture(tmp_path, monkeypatch)
    sid = next(iter(documents))
    # Input is an isolated test capture; production sources are never altered.
    original = documents[sid]
    url = original['metadata']['original_url']
    text = module.body_of(original['markdown'])
    image = png_fixture()
    def capture_with_image(url, directory):
        (directory / 'chart.png').write_bytes(image)
        return {**capture(text)(url, directory), 'assets': [{'status': 'complete', 'relative_path': 'assets/chart.png', 'sha256': hashlib.sha256(image).hexdigest()}]}
    wiki.store.ingest(url, capture_fn=capture_with_image)
    documents[sid] = wiki.store.read(sid)
    deps = {key: {'version_id': doc['metadata']['version_id'], 'hash': digest(doc['markdown'])} for key, doc in documents.items()}
    monkeypatch.setattr(wiki, '_inputs', lambda _: (topic, documents, deps))
    output = wiki.build(topic['id'], LayerModel(), protocol="quote-v1")
    saved = wiki.store.root / 'wiki/versions' / topic['id'] / output['version_id'] / 'sources' / sid / 'assets/chart.png'
    assert saved.read_bytes() == image


def test_leaf_cache_resume_after_service_failure(tmp_path, monkeypatch):
    from knowledge_capture.llm import LLMError
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    class Flaky(LayerModel):
        def complete_json(self, prompt, payload):
            if len(self.calls) == 2:
                self.calls.append(payload)
                raise LLMError('upstream_error', 'temporary overload')
            return super().complete_json(prompt, payload)
    failed = Flaky()
    with pytest.raises(WikiError):
        wiki.build(topic['id'], failed, protocol="quote-v1")
    cache = tmp_path / '.cache/wiki'
    assert len(list((cache / 'leaves').glob('*.json'))) == 2
    log = json.loads(next((cache / 'runs').glob('*.json')).read_text())
    assert log['coverage']['model_calls'] == 3
    assert not log['coverage']['all_source_lines_processed']
    assert log['status'] == 'failed' and not log['publishable']
    assert wiki.list_pages() == []
    model = LayerModel()
    wiki.build(topic['id'], model, protocol="quote-v1")
    coverage = wiki.read(topic['id'])['record']['coverage']
    assert coverage['cache_hits'] == 2
    assert coverage['model_calls'] == len(model.calls)
    assert model.calls[0] == failed.calls[2]
    again = LayerModel()
    wiki.build(topic['id'], again, protocol="quote-v1")
    assert all(p['stage'] == 'reduce' for p in again.calls)


def test_cache_revalidates_ranges_even_with_matching_integrity(tmp_path, monkeypatch):
    wiki, topic, documents = fixture(tmp_path, monkeypatch)
    wiki.build(topic['id'], LayerModel(), protocol="quote-v1")
    for path in (tmp_path / '.cache/wiki/leaves').glob('*.json'):
        record = json.loads(path.read_text())
        cite = record['result']['summary'][0]['evidence'][0]
        if cite['end_line'] < 101:
            cite.update(start_line=101, end_line=101,
                        quote=module.body_of(documents[cite['source_id']]['markdown']).splitlines()[-1])
            record['result_hash'] = digest(module._canonical(record['result']))
            path.write_text(json.dumps(record))
            break
    model = LayerModel()
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], model, protocol="quote-v1")
    assert exc.value.code == 'citation_outside_input'
    assert not model.calls


@pytest.mark.parametrize('changed', ['prompt', 'endpoint', 'body', 'version', 'topic'])
def test_leaf_cache_identity_invalidation(tmp_path, monkeypatch, changed):
    wiki, topic, documents = fixture(tmp_path, monkeypatch)
    model = LayerModel()
    model.cache_identity = {**model.identity, 'endpoint': 'https://fixture/v1/chat/completions'}
    wiki.build(topic['id'], model, protocol="quote-v1")
    model.calls.clear()
    if changed == 'prompt':
        monkeypatch.setattr(module, 'EXTRACT_PROMPT', module.EXTRACT_PROMPT + '\nnew rule')
    elif changed == 'endpoint':
        model.cache_identity['endpoint'] = 'https://fixture/v2/chat/completions'
    elif changed == 'topic':
        topic['name'] += '新主题'
    else:
        doc = next(iter(documents.values()))
        if changed == 'body':
            doc['markdown'] += '\n新增条件仍需核查。'
        else:
            doc['metadata']['version_id'] = 'changed-version'
    wiki.build(topic['id'], model, protocol="quote-v1")
    coverage = wiki.read(topic['id'])['record']['coverage']
    assert coverage['cache_hits'] == 0
    assert any(p.get('stage') == 'extract' for p in model.calls)
