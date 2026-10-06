import socket

import pytest

from workbench_ui_fixture import seed, offline_bindings, LABEL, BASE_URL
from knowledge_capture.discovery import Discovery
from knowledge_capture.llm import CloudClient
from knowledge_capture.processing import Processor
from knowledge_capture.providers import CaptureRouter, ConfiguredSearch
from knowledge_capture.settings import Settings
from knowledge_capture.context_alerts import ContextAlerts
from knowledge_capture.wiki import Wiki


def test_seed_is_labelled_idempotent_with_images_and_candidates(tmp_path):
    store = seed(tmp_path / 'ui-acceptance')
    assert len(store.list_sources()) == 3
    topic = Processor(store).interests()[0]
    assert topic['state'] == 'candidate' and topic['user_source_count'] == 3
    assert len(Wiki(store).list_pages()) == 1
    assert len(ContextAlerts(store).list_alerts()) == 1
    before = (len(store.captures()), len(Processor(store).history()))
    again = seed(tmp_path / 'ui-acceptance')
    assert before == (len(again.captures()), len(Processor(again).history()))
    for source in store.list_sources():
        assert source['title'].startswith(LABEL)
        document = store.read(source['id'])
        from pathlib import Path
        assert (Path(document['path']) / 'assets/demo-chart.png').read_bytes().startswith(b'\x89PNG')


def test_offline_bindings_support_actions_and_discovery_adds_fourth_once(tmp_path):
    store = seed(tmp_path / 'ui-acceptance')
    configuration = Settings(store).provider_configuration()
    topic = Processor(store).interests()[0]
    Processor(store).feedback(topic['id'], 'followed')
    with offline_bindings(store.root):
        assert CloudClient.for_store(store).identity['model'].startswith(LABEL)
        assert ConfiguredSearch(configuration).search('演示')[0]['url'] == BASE_URL + '4'
        result = Discovery(store, configuration).run(topic['id'])
        assert result['status'] == 'complete'
        assert len(store.list_sources()) == 4
        assert Discovery(store, configuration).run(topic['id'])['status'] == 'no_new'
        assert len(store.list_sources()) == 4
        assert ContextAlerts(store).analyze()['status'] == 'complete'
        with socket.socket() as sock, pytest.raises(RuntimeError, match='blocks'):
            sock.connect(('127.0.0.1', 1))
        with pytest.raises(Exception, match='夹具仅接受'):
            CaptureRouter(configuration).capture('https://real-site.example/article', tmp_path)


def test_seed_refuses_other_or_unmarked_libraries(tmp_path):
    with pytest.raises(ValueError):
        seed(tmp_path / 'user-acceptance')
    root = tmp_path / 'ui-acceptance'
    root.mkdir()
    (root / 'existing.txt').write_text('preserve')
    with pytest.raises(ValueError):
        seed(root)
    assert (root / 'existing.txt').read_text() == 'preserve'
