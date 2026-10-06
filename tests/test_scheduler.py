from contextlib import closing
import fcntl
from unittest.mock import Mock

import pytest
from knowledge_capture.scheduler import Scheduler
from knowledge_capture.discovery import DiscoveryError
from test_discovery import followed


def test_due_runs_once_persists_and_repeats_only_at_interval(followed):
    store, topic = followed
    clock = Mock(return_value=1000)
    scheduler = Scheduler(store, clock=clock)
    scheduler.set(topic, 1)
    run = Mock(return_value={'id': 'actual-discovery-id', 'status': 'no_results'})
    assert scheduler.tick(run)['runs'][0]['status'] == 'no_results'
    assert Scheduler(store, clock=clock).tick(run)['status'] == 'idle'
    run.assert_called_once_with(topic)
    clock.return_value = 4600
    assert scheduler.tick(run)['runs'][0]['status'] == 'no_results'
    assert len(scheduler.history()) == 2
    assert scheduler.history()[0]['discovery_id'] == 'actual-discovery-id'


def test_interrupted_requires_manual_resume_and_keeps_history(followed):
    store, topic = followed
    scheduler = Scheduler(store)
    scheduler.set(topic)
    with closing(store._connect()) as db, db:
        db.execute("INSERT INTO schedule_runs VALUES ('old',?,'running',1,NULL,NULL,NULL)", (topic,))
    run = Mock()
    assert scheduler.tick(run)['interrupted_topics'] == [topic]
    run.assert_not_called()
    assert scheduler.list()[0]['enabled'] == 0
    assert scheduler.history()[0]['status'] == 'interrupted'
    scheduler.set(topic)
    assert scheduler.list()[0]['enabled'] == 1


def test_live_lock_does_not_mark_other_work_interrupted(followed):
    store, topic = followed
    scheduler = Scheduler(store)
    scheduler.set(topic)
    with (store.root / '.scheduler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert scheduler.tick()['status'] == 'busy'
    assert scheduler.list()[0]['enabled'] == 1


@pytest.mark.parametrize('partial', [False, True])
def test_error_pauses_no_automatic_retry(followed, partial):
    store, topic = followed
    scheduler = Scheduler(store)
    scheduler.set(topic)
    runner = Mock(return_value={'status': 'partial'}) if partial else Mock(side_effect=DiscoveryError('api_timeout', '调用超时'))
    result = scheduler.tick(runner)
    assert result['runs'][0]['status'] == ('partial' if partial else 'failed')
    assert scheduler.list()[0]['enabled'] == 0
    scheduler.tick(runner)
    assert runner.call_count == 1


def test_paused_interest_will_not_search(followed):
    store, topic = followed
    scheduler = Scheduler(store)
    scheduler.set(topic)
    scheduler.processor.feedback(topic, 'paused')
    runner = Mock()
    assert scheduler.tick(runner)['runs'][0]['status'] == 'paused'
    runner.assert_not_called()
    with pytest.raises(ValueError):
        scheduler.set(topic)


def test_unconfigured_search_records_failure_not_no_new(followed, monkeypatch):
    store, topic = followed
    scheduler = Scheduler(store)
    scheduler.set(topic)
    result = scheduler.tick()
    assert result['runs'][0]['status'] == 'failed'
    assert result['runs'][0]['result']['error_code'] == 'configuration_missing'


def test_new_discovery_triggers_cited_context_check_with_shared_budget(followed, monkeypatch):
    from knowledge_capture.context_alerts import ContextAlerts
    from knowledge_capture.discovery import Discovery
    from knowledge_capture.llm import CloudClient
    from test_context_alerts import Model
    store, topic = followed
    context = ContextAlerts(store)
    context.set_fact('家中路由器', '版本', '7.10')
    source_id = store.list_sources()[0]['id']
    monkeypatch.setattr(Discovery, 'run', lambda self, topic: {'id': 'discovery-test', 'status': 'complete', 'candidates': [{'status': 'added', 'source_id': source_id}]})
    monkeypatch.setattr(CloudClient, 'from_env', lambda: Model())
    scheduler = Scheduler(store)
    scheduler.set(topic)
    result = scheduler.tick()
    assert result['runs'][0]['result']['alerts']['alert_count'] == 1
    assert context.list_alerts()[0]['context_status'] == 'confirmed'
    with closing(store._connect()) as db:
        assert db.execute("SELECT used FROM discovery_budget WHERE kind='model_calls'").fetchone()[0] == 1


def test_failed_context_check_keeps_completed_discovery(followed, monkeypatch):
    from knowledge_capture.context_alerts import ContextAlerts
    from knowledge_capture.discovery import Discovery
    from knowledge_capture.llm import CloudClient, LLMError
    store, topic = followed
    ContextAlerts(store).set_fact('家中路由器', '版本', '7.10')
    source_id = store.list_sources()[0]['id']
    monkeypatch.setattr(Discovery, 'run', lambda self, topic: {'id': 'retained', 'status': 'complete', 'candidates': [{'status': 'added', 'source_id': source_id}]})
    monkeypatch.setattr(CloudClient, 'from_env', Mock(side_effect=LLMError('configuration_missing', '未配置模型')))
    scheduler = Scheduler(store)
    scheduler.set(topic)
    result = scheduler.tick()
    assert result['status'] == 'needs_review'
    assert result['runs'][0]['status'] == 'partial'
    assert result['runs'][0]['result']['id'] == 'retained'
    assert result['runs'][0]['result']['alerts']['status'] == 'failed'
    assert scheduler.history()[0]['discovery_id'] == 'retained'
