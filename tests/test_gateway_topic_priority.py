"""Automatic Wiki selection preserves its three-topic budget and user priorities."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_capture.gateway import _Server


def topic(identifier, count=1, state='candidate', inferred=False):
    return {'id': identifier, 'name': identifier, 'state': state,
            'user_source_count': count, 'inference_ids': ['inference'] if inferred else [],
            'evidence': [{'source_id': 'source'}]}


def run_capture(monkeypatch, topics):
    processor = Mock()
    processor.analyze.return_value = {'id': 'analysis', 'source_version': 'version', 'status': 'complete'}
    processor.read.return_value = {'record': {'topics': [{'name': t['name']} for t in topics if not t['inference_ids']]}}
    processor.interests.return_value = topics
    wiki = Mock()
    wiki.build.side_effect = lambda identifier, **kwargs: {'topic_id': identifier, 'status': 'complete'}
    alerts = Mock()
    alerts.list_facts.return_value = []
    monkeypatch.setattr('knowledge_capture.processing.Processor', lambda store: processor)
    monkeypatch.setattr('knowledge_capture.wiki.Wiki', lambda store: wiki)
    monkeypatch.setattr('knowledge_capture.context_alerts.ContextAlerts', lambda store: alerts)
    monkeypatch.setattr('knowledge_capture.llm.CloudClient.for_store', lambda store: object())
    store = Mock()
    store.read.return_value = {'metadata': {'version_id': 'version'}}
    output = _Server.process_capture(SimpleNamespace(store=store),
        {'source_id': 'source', 'version_id': 'version', 'status': 'complete'})
    return output, processor, wiki


@pytest.mark.parametrize('topics, expected', [
    ([topic('a'), topic('b'), topic('c'), topic('z', count=3, inferred=True)], ['z', 'a', 'b']),
    ([topic('a', count=3, inferred=True), topic('b', count=2), topic('c'), topic('z', state='followed')], ['z', 'a', 'b']),
])
def test_related_topic_priority_without_expanding_budget_or_following(monkeypatch, topics, expected):
    before = deepcopy(topics)
    output, processor, wiki = run_capture(monkeypatch, topics)
    assert [p['topic_id'] for p in output['wiki']['pages']] == expected
    assert wiki.build.call_count == 3
    assert output['wiki']['skipped_topic_ids'] == [t['id'] for t in topics if t['id'] not in expected]
    assert output['wiki']['status'] == 'partial'
    assert topics == before
    processor.feedback.assert_not_called()
