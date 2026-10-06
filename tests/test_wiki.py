from copy import deepcopy
from pathlib import Path

import pytest

from knowledge_capture.processing import Processor
from knowledge_capture.store import Store
from knowledge_capture.wiki import Wiki, WikiError


class Analyzer:
    identity = {'provider': 'test', 'model': 'fixture'}
    def complete_json(self, system, payload):
        return {'summary': [{'text': '更新说明', 'evidence': [{'start_line': 1, 'end_line': 1, 'quote': payload['lines'][0]['text']}]}],
                'key_points': [], 'topics': [{'name': '系统更新', 'reason': '介绍系统更新', 'evidence': [{'start_line': 1, 'end_line': 1, 'quote': payload['lines'][0]['text']}]}], 'questions': []}


class Synthesizer:
    identity = {'provider': 'test', 'model': 'fixture'}
    def __init__(self, change=None):
        self.change = change
    def complete_json(self, system, payload):
        assert '不可信' in system
        citations = [{'source_id': s['source_id'], 'version_id': s['version_id'], 'start_line': 1, 'end_line': 1, 'quote': s['lines'][0]['text']} for s in payload['sources']]
        item = {'text': '不同版本有各自更新条件', 'evidence': citations}
        result = {'summary': [deepcopy(item)], 'agreements': [], 'differences': [deepcopy(item)], 'questions': []}
        if self.change:
            self.change(result)
        return result


def capture(text):
    return lambda url, directory: {'title': '系统资料', 'markdown': text, 'original_url': url, 'final_url': url, 'author': None, 'published_at': None, 'assets': [], 'warnings': [], 'status': 'complete'}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    a = store.ingest('https://example.org/a', capture_fn=capture('版本一需要先备份配置。'))
    b = store.ingest('https://example.org/b', origin='discovery', capture_fn=capture('版本二需要检查插件兼容。'))
    processor = Processor(store)
    processor.analyze(a['source_id'], Analyzer())
    processor.analyze(b['source_id'], Analyzer())
    return store, processor.interests()[0]['id'], a, b


def test_cross_source_wiki_includes_discovery_and_snapshots(setup):
    store, topic, a, b = setup
    wiki = Wiki(store)
    out = wiki.build(topic, Synthesizer(), protocol="quote-v1")
    assert out['status'] == 'complete'
    page = wiki.read(topic)
    assert page['stale'] is False
    assert len(page['record']['dependencies']) == 2
    assert '不同版本' in page['markdown']
    snapshot = store.root / 'wiki/versions' / topic / out['version_id']
    assert (snapshot / 'sources' / b['source_id'] / 'source.md').exists()
    assert (snapshot / 'evidence.md').exists()
    assert len(wiki.list_pages()) == 1


@pytest.mark.parametrize('field,value', [('source_id', 'invented'), ('version_id', 'wrong'), ('start_line', 999), ('quote', '原文不存在的话')])
def test_fabricated_citation_is_rejected(setup, field, value):
    store, topic, *_ = setup
    client = Synthesizer(lambda result: result['summary'][0]['evidence'][0].update({field: value}))
    with pytest.raises(WikiError) as exc:
        Wiki(store).build(topic, client, protocol="quote-v1")
    assert exc.value.code == 'invalid_citation'
    assert Wiki(store).list_pages() == []


def test_difference_requires_two_independent_sources(setup):
    store, topic, *_ = setup
    with pytest.raises(WikiError) as exc:
        Wiki(store).build(topic, Synthesizer(lambda r: r['differences'][0].update(evidence=r['differences'][0]['evidence'][:1])), protocol="quote-v1")
    assert exc.value.code == 'insufficient_sources'


def test_source_update_marks_wiki_stale(setup):
    store, topic, *_ = setup
    wiki = Wiki(store)
    wiki.build(topic, Synthesizer(), protocol="quote-v1")
    store.ingest('https://example.org/a', capture_fn=capture('新版本已经修改升级条件。'))
    assert wiki.read(topic)['stale'] is True


def test_body_tampering_marks_wiki_stale(setup):
    store, topic, a, _ = setup
    wiki = Wiki(store)
    wiki.build(topic, Synthesizer(), protocol="quote-v1")
    source = Path(store.read(a['source_id'])['path']) / 'content.md'
    source.write_text(source.read_text() + '\n手工添加的新资料。\n')
    assert wiki.read(topic)['stale'] is True


def test_manual_edit_not_overwritten_and_history_preserved(setup):
    store, topic, *_ = setup
    wiki = Wiki(store)
    first = wiki.build(topic, Synthesizer(), protocol="quote-v1")
    page = Path(first['path'])
    page.write_text(page.read_text() + '\n人工写入。\n')
    original = page.read_text()
    second = wiki.build(topic, Synthesizer(), protocol="quote-v1")
    assert second['status'] == 'needs_review'
    assert '/candidates/' in second['path']
    assert page.read_text() == original
    assert wiki.read(topic)['modified'] is True
    assert wiki.read(topic)['version_id'] == first['version_id']
    assert len(list((store.root / 'wiki/versions' / topic).iterdir())) == 2


def test_missing_cloud_configuration_never_produces_wiki(setup, monkeypatch):
    for name in ['KC_LLM_BASE_URL', 'KC_LLM_MODEL', 'KC_LLM_API_KEY']:
        monkeypatch.delenv(name, raising=False)
    store, topic, *_ = setup
    with pytest.raises(WikiError) as exc:
        Wiki(store).build(topic, protocol="quote-v1")
    assert exc.value.code == 'configuration_missing'
    assert Wiki(store).list_pages() == []


def test_source_change_during_model_call_prevents_publish(setup):
    store, topic, *_ = setup
    client = Synthesizer(lambda _: store.ingest('https://example.org/a', capture_fn=capture('构建过程中出现新的版本。')))
    with pytest.raises(WikiError) as exc:
        Wiki(store).build(topic, client, protocol="quote-v1")
    assert exc.value.code == 'source_changed'
    assert Wiki(store).list_pages() == []


def test_new_related_analyzed_source_marks_stale(setup):
    store, topic, *_ = setup
    wiki = Wiki(store)
    wiki.build(topic, Synthesizer(), protocol="quote-v1")
    third = store.ingest('https://example.org/c', origin='discovery', capture_fn=capture('版本三需要重新审查硬件支持。'))
    Processor(store).analyze(third['source_id'], Analyzer())
    assert wiki.read(topic)['stale'] is True


def test_source_limit_is_explicit_not_truncated(setup, monkeypatch):
    store, topic, *_ = setup
    wiki = Wiki(store)
    name, documents, deps = wiki._inputs(topic)
    doc = next(iter(documents.values()))
    monkeypatch.setattr(wiki, '_inputs', lambda _: (name, {str(i): doc for i in range(13)}, deps))
    with pytest.raises(WikiError) as exc:
        wiki.build(topic, Synthesizer(), protocol="quote-v1")
    assert exc.value.code == 'input_too_large'
    assert wiki.list_pages() == []


def test_same_content_mirrors_cannot_claim_independent_agreement(setup):
    store, topic, a, b = setup
    from knowledge_capture.wiki import validate
    first = store.read(a['source_id'])
    second = deepcopy(first)
    second['metadata']['version_id'] = 'mirror-version'
    docs = {'first': first, 'second': second}
    citations = [{'source_id': sid, 'version_id': d['metadata']['version_id'], 'start_line': 1, 'end_line': 1, 'quote': '版本一需要先备份配置。'} for sid, d in docs.items()]
    item = {'text': '共同条件', 'evidence': citations}
    with pytest.raises(WikiError) as exc:
        validate({'summary': [item], 'agreements': [item], 'differences': [], 'questions': []}, docs)
    assert exc.value.code == 'insufficient_sources'
