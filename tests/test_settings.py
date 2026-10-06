import copy
import json
import os
import stat
from pathlib import Path

import pytest

from knowledge_capture.settings import Settings, SettingsError, validate_providers, ENV_MODEL
from knowledge_capture.llm import CloudClient, LLMError
from knowledge_capture.connectors import Connector, ConnectorError
from knowledge_capture.store import Store
from knowledge_capture.portable import export_library


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in (*ENV_MODEL, 'TEST_SEARCH_TOKEN', 'TEST_HEADER'):
        monkeypatch.delenv(name, raising=False)


def providers():
    return {'services': {'search_api': {'transport': 'http_json', 'endpoint': 'https://example.org/search', 'token_env': 'TEST_SEARCH_TOKEN', 'headers_env': {'X-Key': 'TEST_HEADER'}}},
            'capabilities': {'search': {'service': 'search_api', 'input_fields': {'query': 'q', 'limit': 'count'}, 'output_fields': {'results': 'data.results'}}}}


def test_processing_preference_persists_without_changing_credentials(tmp_path):
    settings = Settings(tmp_path)
    assert settings.preferences() == {'auto_process': False}
    settings.save_model('https://llm.example/v1', 'model', 'private-secret')
    view = settings.save_preferences(True)
    assert view['preferences']['auto_process'] is True
    assert Settings(tmp_path).preferences()['auto_process'] is True
    assert 'private-secret' not in json.dumps(view)
    assert settings.local_model()['api_key'] == 'private-secret'
    settings.save_preferences(False)
    assert not settings.preferences()['auto_process']
    with pytest.raises(SettingsError):
        settings.save_preferences('false')
    assert not settings.preferences()['auto_process']


def test_model_save_preserve_clear_private(tmp_path):
    settings = Settings(tmp_path)
    result = settings.save_model('https://llm.example/v1', 'model-a', 'private-secret')
    assert result['model']['configured'] and result['model']['source'] == 'local'
    assert 'private-secret' not in json.dumps(result)
    assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
    settings.save_model('https://llm.example/v1', 'model-b')
    assert CloudClient.for_store(tmp_path)._api_key == 'private-secret'
    assert CloudClient.for_store(tmp_path).identity['model'] == 'model-b'
    settings.save_model('https://llm.example/v1', 'model-b', clear_key=True)
    assert not settings.public_view()['model']['key_configured']
    with pytest.raises(LLMError, match='配置'):
        CloudClient.for_store(tmp_path)


def test_environment_is_whole_group_override(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    settings.save_model('https://local.example/v1', 'local-model', 'local-secret')
    monkeypatch.setenv('KC_LLM_MODEL', 'environment-model')
    assert settings.public_view()['model']['environment_incomplete']
    with pytest.raises(LLMError):
        CloudClient.for_store(tmp_path)
    monkeypatch.setenv('KC_LLM_BASE_URL', 'https://env.example/v1')
    monkeypatch.setenv('KC_LLM_API_KEY', 'env-secret')
    assert CloudClient.for_store(tmp_path)._api_key == 'env-secret'
    assert CloudClient.for_store(tmp_path).identity == {'provider': 'env.example', 'model': 'environment-model'}
    assert 'secret' not in json.dumps(settings.public_view())


def test_no_local_model_preserves_from_env_hook(tmp_path, monkeypatch):
    marker = object()
    monkeypatch.setattr(CloudClient, 'from_env', lambda: marker)
    assert CloudClient.for_store(tmp_path) is marker


@pytest.mark.parametrize('base', ['http://llm.example', 'https://user:secret@llm.example', 'https://llm.example?key=private-secret', 'https://llm.example/#key'])
def test_invalid_model_does_not_save_or_echo(tmp_path, base):
    with pytest.raises(SettingsError) as exc:
        Settings(tmp_path).save_model(base, 'model', 'private-secret')
    assert 'private-secret' not in str(exc.value)
    assert not (tmp_path / '.settings.json').exists()


def test_all_service_schemas_and_mappings_validated(tmp_path):
    settings = Settings(tmp_path)
    settings.save_providers(providers())
    assert settings.provider_configuration().data == providers()
    example = json.loads(Path('providers.example.json').read_text())
    validate_providers(example)
    assert settings.public_view()['provider_credentials']['search_api']['token_configured'] is False


@pytest.mark.parametrize('mutate', [
    lambda c: c['services']['search_api'].update(token='private-secret'),
    lambda c: c['services']['search_api'].update(headers={'Authorization': 'private-secret'}),
    lambda c: c['services']['search_api'].update(endpoint='https://example.org?token=private-secret'),
    lambda c: c['services']['search_api'].update(token_env='not a variable'),
    lambda c: c['capabilities']['search'].update(service='missing'),
    lambda c: c['capabilities']['search'].update(constants={'nested': {'api_key': 'private-secret'}}),
    lambda c: c['capabilities']['search'].update(constants={'q': 'fixed'}),
    lambda c: c['capabilities']['search'].update(input_fields={'query': 'same', 'limit': 'same.child'}),
    lambda c: c['capabilities']['search'].update(output_fields={'results': ['invalid']}),
    lambda c: c['capabilities']['search'].update(output_fields={'results': 'data..results'}),
    lambda c: c.update(routing={'default_reader': 'missing'}),
    lambda c: c.update(routing={'capture': [{'hosts': ['https://example.org'], 'reader': 'builtin'}]}),
])
def test_invalid_provider_settings_safe_and_unchanged(tmp_path, mutate):
    settings = Settings(tmp_path)
    settings.save_providers(providers())
    before = settings.providers_path.read_bytes()
    config = providers()
    mutate(config)
    with pytest.raises(SettingsError) as exc:
        settings.save_providers(config)
    assert 'private-secret' not in str(exc.value)
    assert settings.providers_path.read_bytes() == before


def test_provider_secret_injection_environment_precedence_no_echo(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    settings.save_providers(providers())
    settings.save_provider_secret('TEST_SEARCH_TOKEN', 'local-token-secret')
    result = settings.save_provider_secret('TEST_HEADER', 'local-header-secret')
    assert result['provider_credentials']['search_api']['token_configured']
    assert result['provider_credentials']['search_api']['headers_env']['X-Key']['configured']
    assert 'local-token-secret' not in json.dumps(result)
    assert 'local-header-secret' not in settings.providers_path.read_text()
    assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
    monkeypatch.setattr(Connector, '_http', lambda self, payload, headers: headers)
    config = settings.provider_configuration()
    assert config.call('search', {'query': '测试', 'limit': 1}) == {'Authorization': 'Bearer local-token-secret', 'X-Key': 'local-header-secret'}
    assert 'TEST_SEARCH_TOKEN' not in os.environ
    monkeypatch.setenv('TEST_SEARCH_TOKEN', 'environment-token')
    assert config.call('search', {'query': '测试', 'limit': 1})['Authorization'] == 'Bearer environment-token'
    monkeypatch.setenv('TEST_SEARCH_TOKEN', '')
    with pytest.raises(Exception):
        config.call('search', {'query': '测试', 'limit': 1})
    assert not settings.public_view()['provider_credentials']['search_api']['token_configured']


def test_provider_secret_clear_preserve_and_unknown(tmp_path):
    settings = Settings(tmp_path)
    settings.save_providers(providers())
    settings.save_provider_secret('TEST_SEARCH_TOKEN', 'secret')
    settings.save_provider_secret('TEST_SEARCH_TOKEN')
    assert settings.public_view()['provider_credentials']['search_api']['token_configured']
    settings.save_model('https://llm.example/v1', 'model', 'llm-secret')
    assert settings.public_view()['provider_credentials']['search_api']['token_configured']
    settings.save_provider_secret('TEST_SEARCH_TOKEN', clear=True)
    assert not settings.public_view()['provider_credentials']['search_api']['token_configured']
    assert settings.local_model()['api_key'] == 'llm-secret'
    with pytest.raises(SettingsError):
        settings.save_provider_secret('UNRELATED', 'no')


def test_symlink_settings_not_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('unchanged')
    (tmp_path / '.settings.json').symlink_to(outside)
    with pytest.raises(SettingsError):
        Settings(tmp_path).save_model('https://llm.example', 'model', 'secret')
    assert outside.read_text() == 'unchanged'


def test_portable_excludes_local_keys(tmp_path):
    store = Store(tmp_path / 'library')
    settings = Settings(store)
    settings.save_model('https://llm.example', 'model', 'llm-secret')
    settings.save_providers(providers())
    settings.save_provider_secret('TEST_SEARCH_TOKEN', 'provider-secret')
    archive = export_library(store, tmp_path / 'library.zip')
    import zipfile
    with zipfile.ZipFile(archive) as z:
        assert '.settings.json' not in z.namelist() and 'providers.json' not in z.namelist()


def test_concurrent_settings_preserve_distinct_credentials(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    settings = Settings(tmp_path)
    settings.save_providers(providers())
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(Settings(tmp_path).save_provider_secret, 'TEST_SEARCH_TOKEN', 'search-secret'),
                 pool.submit(Settings(tmp_path).save_provider_secret, 'TEST_HEADER', 'header-secret'),
                 pool.submit(Settings(tmp_path).save_model, 'https://llm.example', 'model', 'model-secret')]
        for task in tasks:
            task.result()
    view = settings.public_view()
    assert view['model']['configured']
    assert view['provider_credentials']['search_api']['token_configured']
    assert view['provider_credentials']['search_api']['headers_env']['X-Key']['configured']


def test_model_wait_saved_public_and_legacy_compatible(tmp_path):
    settings = Settings(tmp_path)
    settings.path.write_text(json.dumps({'version':1,'model':{'base_url':'https://model.example/v1','model':'old','api_key':'private'},'provider_secrets':{}}))
    assert settings.public_view()['model']['timeout_seconds'] == 180
    assert CloudClient.for_store(tmp_path).timeout_seconds == 180
    settings.save_model('https://model.example/v1','new',timeout_seconds=420)
    assert settings.public_view()['model']['timeout_seconds'] == 420
    assert CloudClient.for_store(tmp_path).timeout_seconds == 420
    settings.save_model('https://model.example/v1','newer')
    assert CloudClient.for_store(tmp_path).timeout_seconds == 420
    assert settings.local_model()['api_key'] == 'private'


@pytest.mark.parametrize('value', [9, 601, True, 180.5, '180'])
def test_invalid_saved_wait_preserves_previous_settings(tmp_path, value):
    settings = Settings(tmp_path)
    settings.save_model('https://model.example/v1','model','private',timeout_seconds=200)
    before = settings.path.read_bytes()
    with pytest.raises(SettingsError):
        settings.save_model('https://model.example/v1','model',timeout_seconds=value)
    assert settings.path.read_bytes() == before
