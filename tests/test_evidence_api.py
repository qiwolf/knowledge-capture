from pathlib import Path

import pytest

from knowledge_capture.store import Store
from knowledge_capture.processing import Processor
from knowledge_capture.wiki import Wiki
from knowledge_capture.context_alerts import ContextAlerts
from knowledge_capture.evidence_api import read_source, read_analysis, read_wiki, read_alert, EvidenceError


def capture(text):
    return lambda url, directory: {'title': '更新资料', 'markdown': text, 'original_url': url, 'final_url': url,
        'author': None, 'published_at': None, 'assets': [], 'warnings': [], 'status': 'complete'}


class Model:
    identity = {'provider': 'test', 'model': 'fixture'}
    def complete_json(self, system, payload):
        if 'lines' in payload:
            evidence = [{'start_line': 1, 'end_line': 1, 'quote': payload['lines'][0]['text']}]
            return {'summary': [{'text': '更新资料', 'evidence': evidence}], 'key_points': [],
                    'topics': [{'name': '更新条件', 'reason': '讨论更新', 'evidence': evidence}], 'questions': []}
        citations = [{'source_id': s['source_id'], 'version_id': s['version_id'], 'start_line': 1, 'end_line': 1,
                      'quote': s['lines'][0]['text']} for s in payload['sources']]
        if 'facts' in payload:
            return {'alerts': [{'title': '核实更新条件', 'detail': '需要核对背景与公告', 'suggested_action': '人工核实',
                                'fact_ids': [payload['facts'][0]['id']], 'evidence': citations}]}
        for citation in citations:
            citation.pop('quote')
        return {'summary': [{'text': '更新需要核查条件', 'evidence': citations}], 'agreements': [], 'differences': [], 'questions': []}


@pytest.fixture
def ready(tmp_path):
    store = Store(tmp_path)
    source = store.ingest('https://example.org/a', capture_fn=capture('旧版系统需要先核查更新条件。'))
    processor = Processor(store)
    analysis = processor.analyze(source['source_id'], Model())
    topic = processor.interests()[0]['id']
    wiki = Wiki(store).build(topic, Model())
    context = ContextAlerts(store)
    context.set_fact('路由器', '系统版本', '1.0')
    alert = context.analyze(client=Model())
    return store, source, analysis, topic, wiki, alert


def test_source_read_matches_requested_version(ready):
    store, source, analysis, *_ = ready
    new = store.ingest('https://example.org/a', capture_fn=capture('新版系统条件不同，需要重新核对。'))
    current = read_source(store, source['source_id'])
    assert current['source']['metadata']['version_id'] == new['version_id']
    assert current['latest_analysis'] is None
    old = read_source(store, source['source_id'], source['version_id'])
    assert old['latest_analysis']['record']['id'] == analysis['id']
    assert old['analysis_stale'] is False  # Matches the requested historical body.
    assert old['latest_analysis']['stale'] is True  # Not the current source revision.


def test_modified_body_does_not_hide_stale_analysis(ready):
    store, source, *_ = ready
    file = Path(store.read(source['source_id'])['path']) / 'content.md'
    file.write_text(file.read_text() + '\n用户改写了正文。\n')
    result = read_source(store, source['source_id'])
    assert result['analysis_stale'] is True
    assert result['latest_analysis']['stale'] is True
    historical = result['latest_analysis']['sources'][source['source_id']]['markdown']
    assert '用户改写' not in historical


def test_all_evidence_readers_preserve_old_source_after_current_changes(ready):
    store, source, analysis, topic, wiki, alert = ready
    store.ingest('https://example.org/a', capture_fn=capture('新版本已撤回原来的更新说明。'))
    for result in [read_analysis(store, analysis['id']), read_wiki(store, topic), read_alert(store, alert['id'])]:
        assert result['stale'] is True
        assert result['evidence_markdown']
        snapshot = result['sources'][source['source_id']]
        assert snapshot['version_id'] == source['version_id']
        assert snapshot['lines'][0]['text'] == '旧版系统需要先核查更新条件。'
        assert '撤回' not in snapshot['markdown']


def test_wiki_candidate_can_be_read_by_exact_version(ready):
    store, _, _, topic, first, _ = ready
    file = Path(first['path'])
    file.write_text(file.read_text() + '\n人工备注。\n')
    candidate = Wiki(store).build(topic, Model())
    assert candidate['status'] == 'needs_review'
    result = read_wiki(store, topic, candidate['version_id'])
    assert result['status'] == 'needs_review'
    assert result['evidence_markdown']
    assert read_wiki(store, topic)['version_id'] == first['version_id']


def test_tampered_snapshot_is_not_served_as_verified_history(ready):
    store, source, analysis, *_ = ready
    result = read_analysis(store, analysis['id'])
    path = Path(result['path']) / 'source.md'
    path.write_text('历史内容被修改')
    with pytest.raises(EvidenceError) as exc:
        read_analysis(store, analysis['id'])
    assert exc.value.code == 'evidence_changed'


def test_missing_evidence_does_not_fall_back_to_current(ready):
    store, _, analysis, *_ = ready
    result = read_analysis(store, analysis['id'])
    (Path(result['path']) / 'evidence.md').unlink()
    with pytest.raises(EvidenceError) as exc:
        read_analysis(store, analysis['id'])
    assert exc.value.code == 'evidence_missing'


def test_symlink_evidence_rejected(ready, tmp_path):
    store, _, analysis, *_ = ready
    result = read_analysis(store, analysis['id'])
    file = Path(result['path']) / 'evidence.md'
    file.unlink()
    file.symlink_to(tmp_path / 'index.sqlite3')
    with pytest.raises(EvidenceError) as exc:
        read_analysis(store, analysis['id'])
    assert exc.value.code == 'invalid_path'


@pytest.mark.parametrize('reader,args', [(read_source,('../bad',)), (read_source,('a'*24,'../bad')),
    (read_analysis,('../bad',)), (read_wiki,('../bad',)), (read_alert,('../bad',))])
def test_invalid_identifiers_rejected(tmp_path, reader, args):
    with pytest.raises(EvidenceError) as exc:
        reader(Store(tmp_path), *args)
    assert exc.value.code == 'invalid_id'
