from datetime import datetime, timedelta, timezone

import pytest

from knowledge_capture.context_alerts import ContextAlerts, ContextError
from knowledge_capture.store import Store


def capture(text):
    return lambda url, directory: {'title': '版本安全公告', 'markdown': text, 'original_url': url, 'final_url': url, 'author': None, 'published_at': None, 'assets': [], 'warnings': [], 'status': 'complete'}


class Model:
    identity = {'provider': 'fixture', 'model': 'test'}
    def __init__(self, change=None): self.change = change
    def complete_json(self, system, payload):
        assert '不可信' in system and '词典序' in system
        source = payload['sources'][0]
        result = {'alerts': [{'title': '请核实路由器版本适用性', 'detail': '公告与记录产品有关，是否适用仍需核实。', 'suggested_action': '核实当前版本和公告适用范围。', 'fact_ids': [payload['facts'][0]['id']], 'evidence': [{'source_id': source['source_id'], 'version_id': source['version_id'], 'start_line': 1, 'end_line': 1, 'quote': source['lines'][0]['text']}]}]}
        if self.change: self.change(result)
        return result


@pytest.fixture
def ready(tmp_path):
    store = Store(tmp_path)
    source = store.ingest('https://example.org/advisory', capture_fn=capture('RouterOS 某些版本需要核查安全更新适用范围。'))
    context = ContextAlerts(store)
    fact = context.set_fact('路由器A', '版本', '7.10')
    return store, context, source, fact


def test_explicit_fact_and_advisory_are_not_device_action(ready):
    store, context, source, fact = ready
    result = context.analyze(client=Model())
    assert result['status'] == 'complete'
    alert = context.list_alerts()[0]
    assert alert['status'] == 'needs_review'
    assert alert['assessment'] == 'model_candidate'
    assert alert['context_status'] == 'confirmed'
    assert alert['action_executed'] is False and alert['stale'] is False
    read = context.read(result['id'])
    assert 'evidence.md#' in read['markdown']
    assert read['record']['facts'][0]['value'] == '7.10'


@pytest.mark.parametrize('state', ['unknown', 'stale'])
def test_unknown_or_old_context_cannot_be_labelled_confirmed(ready, state):
    store, context, _, _ = ready
    if state == 'unknown':
        context.set_fact('路由器A', '版本', status='unknown')
    else:
        context.set_fact('路由器A', '版本', '7.10', confirmed_at=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat())
    assert context.analyze(client=Model())['alerts'][0]['context_status'] == state


def test_changed_fact_invalidates_previous_candidate_but_keeps_snapshot(ready):
    _, context, _, old = ready
    result = context.analyze(client=Model())
    new = context.set_fact('路由器A', '版本', '7.20')
    assert new['id'] == old['id'] and new['revision'] != old['revision']
    assert context.list_alerts()[0]['status'] == 'needs_refresh'
    assert context.read(result['id'])['record']['facts'][0]['value'] == '7.10'


def test_changed_source_invalidates_candidate(ready):
    store, context, _, _ = ready
    context.analyze(client=Model())
    store.ingest('https://example.org/advisory', capture_fn=capture('公告已更新，需要检查新的适用条件。'))
    assert context.list_alerts()[0]['stale'] is True


@pytest.mark.parametrize('field,value', [('source_id','fake'),('version_id','fake'),('start_line',999),('quote','凭空捏造引用')])
def test_fabricated_source_evidence_rejected(ready, field, value):
    _, context, _, _ = ready
    with pytest.raises(ContextError) as exc:
        context.analyze(client=Model(lambda r:r['alerts'][0]['evidence'][0].update({field:value})))
    assert exc.value.code == 'invalid_citation'
    assert context.list_alerts() == []


def test_invented_user_fact_reference_rejected(ready):
    _, context, _, _ = ready
    with pytest.raises(ContextError) as exc:
        context.analyze(client=Model(lambda r:r['alerts'][0].update(fact_ids=['invented'])))
    assert exc.value.code == 'invalid_context_reference'


def test_missing_configuration_fails_without_alerts(ready, monkeypatch):
    _, context, _, _ = ready
    for name in ['KC_LLM_BASE_URL','KC_LLM_MODEL','KC_LLM_API_KEY']: monkeypatch.delenv(name, raising=False)
    with pytest.raises(ContextError) as exc: context.analyze()
    assert exc.value.code == 'configuration_missing'
    assert context.list_alerts() == []


def test_changed_context_during_call_rejects_publish(ready):
    _, context, _, _ = ready
    with pytest.raises(ContextError) as exc:
        context.analyze(client=Model(lambda _: context.set_fact('路由器A','版本','7.20')))
    assert exc.value.code == 'inputs_changed'
    assert context.list_alerts() == []


def test_no_candidate_not_claimed_no_risk(ready):
    _, context, _, _ = ready
    result = context.analyze(client=Model(lambda r:r.update(alerts=[])))
    assert result['alert_count'] == 0
    assert '不代表已证明不存在风险' in context.read(result['id'])['markdown']


@pytest.mark.parametrize('kwargs', [{'valid_days':0}, {'status':'invented'}, {'confirmed_at':'2026-10-06'}, {'confirmed_at':'2099-01-01T00:00:00Z'}])
def test_invalid_explicit_context_rejected(ready, kwargs):
    _, context, _, _ = ready
    with pytest.raises(ContextError): context.set_fact('设备','版本','1',**kwargs)


def test_same_evidence_different_wording_deduplicates_to_latest(ready):
    _, context, _, _ = ready
    first = context.analyze(client=Model())
    second = context.analyze(client=Model(lambda r:r['alerts'][0].update(title='改了标题但同一风险', detail='用不同文字描述同一份证据')))
    assert first['alerts'][0]['fingerprint'] == second['alerts'][0]['fingerprint']
    alerts = context.list_alerts()
    assert len(alerts) == 1
    assert alerts[0]['id'] == second['alerts'][0]['id']
    assert context.read(first['id'])['record']['alerts'][0]['title'] != alerts[0]['title']


def test_ignore_persists_across_rerun_and_does_not_modify_original_file(ready):
    _, context, _, _ = ready
    from pathlib import Path
    first = context.analyze(client=Model())
    record = Path(first['path']).parent / 'record.json'
    before = record.read_bytes()
    second = context.analyze(client=Model())
    # Old UI ID resolves to the same fingerprint after a newer run.
    feedback = context.feedback(first['alerts'][0]['id'], 'dismissed')
    assert feedback['id'] == second['alerts'][0]['id']
    assert feedback['feedback_state'] == 'dismissed' and feedback['actionable'] is False
    context.analyze(client=Model())
    assert len(context.list_alerts()) == 1
    assert context.list_alerts()[0]['feedback_state'] == 'dismissed'
    assert record.read_bytes() == before
    assert context.feedback(first['alerts'][0]['id'], 'open')['actionable'] is True


@pytest.mark.parametrize('change', ['context','source'])
def test_new_dependency_revision_does_not_inherit_feedback(ready, change):
    store, context, _, _ = ready
    first = context.analyze(client=Model())
    context.feedback(first['alerts'][0]['id'], 'acknowledged')
    if change == 'context':
        context.set_fact('路由器A', '版本', '7.20')
    else:
        store.ingest('https://example.org/advisory', capture_fn=capture('RouterOS 公告更新后需要重新核查适用范围。'))
    second = context.analyze(client=Model())
    assert second['alerts'][0]['fingerprint'] != first['alerts'][0]['fingerprint']
    current, old = context.list_alerts()
    assert current['feedback_state'] == 'open' and current['stale'] is False
    assert old['feedback_state'] == 'acknowledged' and old['stale'] is True


def test_expired_context_requires_refresh_and_new_analysis_not_old_ack(ready, monkeypatch):
    _, context, _, _ = ready
    first = context.analyze(client=Model())
    context.feedback(first['alerts'][0]['id'], 'acknowledged')
    import knowledge_capture.context_alerts as module
    real_datetime = datetime
    class FutureDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(days=31)
    monkeypatch.setattr(module, 'datetime', FutureDateTime)
    previous = context.list_alerts()[0]
    assert previous['stale'] is True and previous['status'] == 'needs_refresh'
    second = context.analyze(client=Model())
    assert second['alerts'][0]['context_status'] == 'stale'
    assert second['alerts'][0]['fingerprint'] != first['alerts'][0]['fingerprint']
    assert context.list_alerts()[0]['feedback_state'] == 'open'


def test_invalid_feedback_rejected(ready):
    _, context, _, _ = ready
    with pytest.raises(ContextError) as error:
        context.feedback('absent', 'dismissed')
    assert error.value.code == 'alert_missing'
    with pytest.raises(ContextError) as error:
        context.feedback('absent', 'fixed')
    assert error.value.code == 'invalid_feedback'
