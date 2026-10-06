"""Offline entrypoint integration; no real engine/model requests."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_capture.providers import Configuration, ConfiguredSearch, CaptureRouter
from knowledge_capture.store import Store
from knowledge_capture.scheduler import Scheduler
from knowledge_capture.workbench_api import Workbench, WorkbenchError


class SelectionClient:
    identity = {'model': 'offline-selection', 'provider': 'fixture'}
    def __init__(self, blocked=False):
        self.calls = 0
        self.blocked = blocked
    def complete_json(self, system, payload):
        self.calls += 1
        def ref(text):
            unit = next(u for u in payload['units'] if u['text'] == text)
            return {'unit_id': unit['id'], 'start': 0, 'end': len(text)}
        empty = {'classification': 'captcha', 'title': None, 'body': [], 'results': [], 'images': []}
        if self.blocked:
            return empty
        if payload['purpose'] == 'search':
            return {**empty, 'classification': 'search', 'results': [{'title': ref('原始标题'),
                'url_id': payload['links'][0]['id'], 'description': None}]}
        body = next(u['text'] for u in payload['units'] if len(u['text']) > 100)
        return {**empty, 'classification': 'article', 'title': ref('原始标题'), 'body': [ref(body)]}


def configuration(response, kind='reader'):
    connector = Mock()
    connector.call.return_value = response
    name = 'search' if kind == 'search' else 'engine_reader'
    config = Configuration({'services': {'engine': {'transport': 'http-json'}},
        'capabilities': {name: {'service': 'engine', 'auto_content': True,
                              'input_fields': {'query': 'q', 'limit': None} if kind == 'search' else {}}},
        'routing': {'default_reader': name}}, connector_factory=lambda spec: connector)
    return config, connector


def test_auto_search_selects_original_fields_and_archives_response(tmp_path):
    store = Store(tmp_path)
    response = {'opaque': [{'heading': '原始标题', 'destination': 'https://example.org/a'}]}
    config, connector = configuration(response, 'search')
    client = SelectionClient()
    output = ConfiguredSearch(config, store=store, client=client).search('主题', 3)
    assert output == [{'title': '原始标题', 'url': 'https://example.org/a', 'description': ''}]
    connector.call.assert_called_once_with({'q': '主题'})
    assert client.calls == 1
    assert json.loads(next((store.root / 'engine_responses').glob('*/response.txt')).read_text()) == response


def test_auto_reader_retains_raw_and_does_not_interpret_embedded_markdown(tmp_path, monkeypatch):
    store = Store(tmp_path / 'library')
    text = '这是原始正文，讨论资料采集、来源验证、版本留存、证据检查、业务边界与失败处理。' * 4 + '\n![误识别图片](https://example.org/unsafe.png)'
    response = {'heading': '原始标题', 'content': text}
    config, _ = configuration(response)
    monkeypatch.setattr('knowledge_capture.providers._target', lambda url: None)
    loader = Mock(side_effect=AssertionError('unselected image must not load'))
    monkeypatch.setattr('knowledge_capture.capture._request', loader)
    assets = tmp_path / 'capture' / 'assets'
    assets.mkdir(parents=True)
    result = CaptureRouter(config, store=store, client=SelectionClient()).capture('https://example.org/a', assets)
    assert result['title'] == '原始标题'
    assert result['assets'] == []
    assert result['acquisition']['normalization']['semantic_verification'] is False
    assert json.loads((assets.parent / 'raw' / 'response.txt').read_text()) == response
    loader.assert_not_called()


def test_blocked_response_is_failed_and_raw_still_exists(tmp_path, monkeypatch):
    from knowledge_capture.engine_content import ContentNormalizationError
    store = Store(tmp_path / 'library')
    config, _ = configuration({'message': '验证码'})
    monkeypatch.setattr('knowledge_capture.providers._target', lambda url: None)
    with pytest.raises(ContentNormalizationError):
        CaptureRouter(config, store=store, client=SelectionClient(blocked=True)).capture('https://example.org/a', tmp_path / 'assets')
    assert len(list((store.root / 'engine_responses').glob('*/response.txt'))) == 1
    assert len(list((store.root / 'engine_responses').glob('*/failure.json'))) == 1


def test_engine_actions_dispatch_and_override_guard(tmp_path, monkeypatch):
    fake = Mock()
    fake.save_engine.return_value = {'engines': {'reader': {'configured': True}}}
    fake.discover_engine.return_value = {'engines': {'reader': {'status': 'adapted'}}}
    monkeypatch.setattr('knowledge_capture.settings.Settings', lambda root: fake)
    bench = object.__new__(Workbench)
    bench.store = SimpleNamespace(root=tmp_path)
    bench.server = SimpleNamespace(explicit_configuration=None)
    args = {'kind': 'reader', 'transport': 'api', 'endpoint': 'https://example.org', 'api_key': 'private'}
    assert bench._execute('settings-engine', args)['engines']['reader']['configured']
    fake.save_engine.assert_called_once_with(**args)
    assert bench._execute('settings-engine-discover', {'kind': 'reader'})['status'] == 'complete'
    fake.discover_engine.assert_called_once_with(kind='reader')
    fake.discover_engine.return_value = {'engines': {'reader': {'status': 'unsupported'}}}
    assert bench._execute('settings-engine-discover', {'kind': 'reader'})['status'] == 'partial'
    bench.server.explicit_configuration = object()
    with pytest.raises(WorkbenchError):
        bench._execute('settings-engine', args)


def test_long_running_scheduler_refreshes_saved_configuration(tmp_path):
    store = Store(tmp_path)
    configurations = iter(['first', 'second'])
    scheduler = Scheduler(store, configuration_loader=lambda: next(configurations))
    scheduler.tick()
    assert scheduler.configuration == 'first'
    scheduler.tick()
    assert scheduler.configuration == 'second'


def test_reader_to_store_keeps_version_raw_after_temporary_capture_removed(tmp_path, monkeypatch):
    import hashlib
    store = Store(tmp_path / 'library')
    response = {'heading': '原始标题', 'content': '资料必须保留出处、完整版本、原始图片与正文内容；整理结论引用可核验的实际原文，不能把验证页或错误响应当作成功采集。' * 4}
    config, _ = configuration(response)
    monkeypatch.setattr('knowledge_capture.providers._target', lambda url: None)
    captured = store.ingest('https://example.org/a',
        capture_fn=CaptureRouter(config, store=store, client=SelectionClient()).capture)
    document = store.read(captured['source_id'])
    version = Path(document['path'])
    normalization = document['metadata']['acquisition']['normalization']
    assert normalization['raw_path'] == 'raw/response.txt'
    assert normalization['archive_path'] == 'raw'
    raw = (version / normalization['raw_path']).read_bytes()
    assert json.loads(raw) == response
    assert hashlib.sha256(raw).hexdigest() == normalization['raw_sha256']
    assert (version / 'raw' / 'inventory.json').is_file()
    assert (version / 'raw' / 'selection.json').is_file()
    assert not list(store.root.glob('.capture-*'))


def test_raw_evidence_survives_source_export_and_full_library_move_then_detects_tamper(tmp_path, monkeypatch):
    import shutil
    import zipfile
    from knowledge_capture.portable import export_library, restore_library
    store = Store(tmp_path / 'original')
    response = {'heading': '原始标题', 'content': '知识库迁移需要保存版本原始响应、模型选择区间、图片校验清单和文件哈希；恢复时重新检查所有证据文件，不能依赖已经删除的旧目录。' * 4}
    config, _ = configuration(response)
    monkeypatch.setattr('knowledge_capture.providers._target', lambda url: None)
    captured = store.ingest('https://example.org/a',
        capture_fn=CaptureRouter(config, store=store, client=SelectionClient()).capture)
    sid = captured['source_id']
    source_zip = store.export(sid, tmp_path / 'source.zip')
    with zipfile.ZipFile(source_zip) as archive:
        assert json.loads(archive.read(sid + '/raw/response.txt')) == response
        assert sid + '/raw/selection.json' in archive.namelist()
    full_zip = export_library(store, tmp_path / 'library.zip')
    shutil.rmtree(store.root)
    restored = Store(restore_library(full_zip, tmp_path / 'restored'))
    document = restored.read(sid)
    raw = Path(document['path']) / 'raw' / 'response.txt'
    assert json.loads(raw.read_text()) == response
    restored.export(sid, tmp_path / 'restored-source.zip')
    raw.write_text('tampered', encoding='utf-8')
    with pytest.raises(ValueError, match='完整性'):
        restored.export(sid, tmp_path / 'must-not-publish.zip')
    with pytest.raises(ValueError, match='完整性'):
        export_library(restored, tmp_path / 'must-not-publish-library.zip')
    assert not (tmp_path / 'must-not-publish.zip').exists()
    assert not (tmp_path / 'must-not-publish-library.zip').exists()


@pytest.mark.parametrize('reader_status, model_configured, expected', [
    ('adapted', True, True), ('adapted', False, False), ('configured', True, False),
    ('not_configured', True, False),
])
def test_new_reader_mode_automatically_continues_capture_and_overview_matches(
        tmp_path, monkeypatch, reader_status, model_configured, expected):
    from knowledge_capture.gateway import create_server
    from knowledge_capture.settings import Settings
    original = Settings.public_view
    def view(settings):
        output = original(settings)
        output['engines'] = {'reader': {'status': reader_status}}
        output['model']['configured'] = model_configured
        return output
    monkeypatch.setattr(Settings, 'public_view', view)
    store = Store(tmp_path)
    store.ingest = Mock(return_value={'status': 'complete'})
    server = create_server(store, port=0)
    server.process_capture = Mock(return_value={'status': 'complete', 'analysis': {'status': 'complete'}})
    try:
        assert server.effective_auto_process() is expected
        assert server.workbench.overview()['capabilities']['auto_process'] is expected
        submitted = server.enqueue({'url': 'https://example.org/a', 'note': '', 'origin': 'user'}, None)
        server.executor.shutdown(wait=True)
        assert server.job(submitted['id'])['status'] == 'complete'
        assert server.process_capture.call_count == int(expected)
        assert Settings(store).preferences() == {'auto_process': False}
    finally:
        server.server_close()


def test_unadapted_reader_blocks_before_url_resolution_or_network(tmp_path, monkeypatch):
    from knowledge_capture.providers import ProviderError
    config, connector = configuration({})
    config.reader_error = '配置的反爬引擎尚未适配'
    resolve = Mock(side_effect=AssertionError('must not resolve'))
    monkeypatch.setattr('knowledge_capture.providers._target', resolve)
    with pytest.raises(ProviderError, match='尚未适配'):
        CaptureRouter(config).capture('https://example.org/a', tmp_path / 'assets')
    resolve.assert_not_called()
    connector.call.assert_not_called()
