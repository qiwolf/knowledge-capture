import json
import stat
import pytest
from knowledge_capture.engines import discover, UnsupportedEngine
from knowledge_capture.settings import Settings, SettingsError
from knowledge_capture.connectors import ConnectorError


def schema_document(server='/api', security=None):
    document = {'openapi': '3.0.3', 'servers': [{'url': server}], 'paths': {
        '/search': {'get': {'operationId': 'search', 'parameters': [
            {'name': 'q', 'in': 'query', 'required': True, 'schema': {'type': 'string'}}],
            'responses': {'200': {'description': 'results'}}}}}}
    if security:
        document.update(security=[{'auth': []}], components={'securitySchemes': {'auth': security}})
    return document


def test_engine_saved_private_and_key_not_reused_cross_origin(tmp_path):
    settings = Settings(tmp_path)
    result = settings.save_engine('search', 'api', 'https://one.example', 'private-key')
    assert result['engines']['search']['status'] == 'configured'
    assert 'private-key' not in json.dumps(result)
    assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
    settings.save_engine('search', 'api', 'https://one.example/search')
    assert settings._document()['engines']['search']['api_key'] == 'private-key'
    settings.save_engine('search', 'api', 'https://two.example')
    assert settings._document()['engines']['search']['api_key'] == ''
    with pytest.raises(SettingsError):
        settings.save_engine('reader', 'api', 'https://x.example?api_key=secret')
    with pytest.raises(SettingsError):
        settings.save_engine('reader', 'api', 'http://public.example')
    settings.save_engine('reader', 'api', 'http://127.0.0.1:8080')


def test_known_preset_combines_engines_and_legacy(tmp_path):
    settings = Settings(tmp_path)
    settings.save_engine('search', 'api', 'https://api.search.brave.com', 'search-secret')
    settings.save_engine('reader', 'api', 'https://api.firecrawl.dev', 'reader-secret')
    settings.discover_engine('search')
    result = settings.discover_engine('reader')
    assert all(entry['status'] == 'adapted' for entry in result['engines'].values())
    config = settings.provider_configuration()
    assert config.data['routing']['default_reader'] == 'engine_reader'
    assert config.data['capabilities']['search']['auto_content'] is True
    assert 'search-secret' not in json.dumps(result)
    assert 'reader-secret' not in json.dumps(result)
    service = config.data['services']['engine_search']
    connector = config.connector_factory(service)
    assert connector._credential_values['KC_ENGINE_SEARCH_KEY'] == 'search-secret'


def test_openapi_document_url_only_fetches_metadata_and_maps_query():
    calls = []
    class Fake:
        def __init__(self, config, **kwargs):
            calls.append(config)
        def call(self, payload):
            assert payload == {}
            return schema_document()
    result = discover('search', 'api', 'https://example.org/openapi.json', connector_factory=Fake)
    assert len(calls) == 1 and calls[0]['method'] == 'GET'
    assert result['services']['engine_search']['endpoint'] == 'https://example.org/api/search'
    assert result['capabilities']['search']['input_fields'] == {'query': 'q', 'limit': None}


def test_openapi_does_not_send_key_to_docs_or_foreign_servers():
    calls = []
    class Fake:
        def __init__(self, config, **kwargs):
            calls.append((config, kwargs))
        def call(self, payload):
            return schema_document('https://evil.example', {'type': 'http', 'scheme': 'bearer'})
    with pytest.raises(UnsupportedEngine):
        discover('search', 'api', 'https://example.org/openapi.json', 'secret', connector_factory=Fake)
    assert 'secret' not in json.dumps(calls)
    assert 'token_env' not in calls[0][0]


def test_openapi_header_auth_and_ambiguity():
    document = schema_document(security={'type': 'apiKey', 'in': 'header', 'name': 'X-Key'})
    class Fake:
        def __init__(self, *args, **kwargs):
            pass
        def call(self, payload):
            return document
    result = discover('search', 'api', 'https://example.org/openapi.json', 'secret', connector_factory=Fake)
    assert result['services']['engine_search']['headers_env'] == {'X-Key': 'KC_ENGINE_KEY'}
    document['paths']['/another/search'] = document['paths']['/search']
    with pytest.raises(UnsupportedEngine):
        discover('search', 'api', 'https://example.org/openapi.json', 'secret', connector_factory=Fake)


def test_mcp_discovery_lists_only_readonly_unique_tool():
    tools = [{'name': 'web_search', 'annotations': {'readOnlyHint': True}, 'inputSchema': {
        'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}]
    class Fake:
        def __init__(self, *args, **kwargs):
            pass
        def list_tools(self):
            return tools
        def call(self, *args):
            pytest.fail('discovery must not invoke tools')
    result = discover('search', 'mcp', 'https://example.org/mcp', connector_factory=Fake)
    assert result['services']['engine_search']['mcp_tool'] == 'web_search'
    tools[0]['annotations'] = {}
    with pytest.raises(UnsupportedEngine):
        discover('search', 'mcp', 'https://example.org/mcp', connector_factory=Fake)
    tools[0]['annotations'] = {'readOnlyHint': True}
    tools.append(dict(tools[0], name='other_search'))
    with pytest.raises(UnsupportedEngine):
        discover('search', 'mcp', 'https://example.org/mcp', connector_factory=Fake)


def test_unsupported_and_failed_are_visible_without_fake_adapter(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    settings.save_engine('reader', 'api', 'https://example.org')
    def unsupported(*args):
        raise UnsupportedEngine('服务尚未适配')
    monkeypatch.setattr('knowledge_capture.engines.discover', unsupported)
    assert settings.discover_engine('reader')['engines']['reader']['status'] == 'unsupported'
    assert settings.provider_configuration().reader_error
    assert settings.public_view()['engines']['reader']['status'] == 'unsupported'
    def failure(*args):
        raise ConnectorError('authentication_failed', '认证失败')
    monkeypatch.setattr('knowledge_capture.engines.discover', failure)
    assert settings.discover_engine('reader')['engines']['reader']['status'] == 'failed'


def test_real_mcp_sdk_discovery_only_lists_and_follows_bounded_cursor():
    import httpx
    from unittest.mock import patch
    from knowledge_capture.connectors import Connector
    methods = []
    async def handle(request):
        message = json.loads(request.content)
        methods.append(message['method'])
        if message['method'] == 'initialize':
            value = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'mock', 'version': '1'}}
        elif message['method'] == 'notifications/initialized':
            return httpx.Response(202)
        elif message['method'] == 'tools/list':
            if message.get('params', {}).get('cursor'):
                value = {'tools': []}
            else:
                value = {'tools': [{'name': 'search', 'inputSchema': {'type': 'object'}, 'annotations': {'readOnlyHint': True}}], 'nextCursor': 'next'}
        else:
            pytest.fail('discovery must never call tools')
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': value})
    with patch('httpx.AsyncHTTPTransport', return_value=httpx.MockTransport(handle)):
        result = Connector({'transport': 'mcp', 'endpoint': 'https://example.org/mcp', 'mcp_tool': '__discovery__'}).list_tools()
    assert [tool['name'] for tool in result] == ['search']
    assert methods == ['initialize', 'notifications/initialized', 'tools/list', 'tools/list']


@pytest.mark.parametrize('constraint', [{'enum':['fixed']},{'const':'fixed'},{'pattern':'^fixed$'},{'oneOf':[{'type':'string'}]}])
def test_constrained_primary_is_not_falsely_adapted(constraint):
    from knowledge_capture.engines import _mapping
    schema={'type':'object','properties':{'query':{'type':'string',**constraint}},'required':['query']}
    assert _mapping('search',schema) is None


def test_limit_constraint_and_nested_required_default_are_explicitly_unsupported():
    from knowledge_capture.engines import _mapping
    assert _mapping('search', {'type':'object','properties':{'query':{'type':'string'},'limit':{'type':'integer','maximum':3}}}) is None
    assert _mapping('search', {'type':'object','properties':{'query':{'type':'string'},'region':{'type':'string','default':'cn'}},'required':['query','region']}) is None
    assert _mapping('reader', {'type':'object','properties':{'request':{'type':'object','properties':{'url':{'type':'string'}}}},'required':['request']}) is None


@pytest.mark.parametrize('level',['path','operation'])
def test_openapi_overridden_server_is_never_ignored(level):
    from knowledge_capture.engines import _openapi
    document=schema_document()
    target=document['paths']['/search']
    if level=='operation': target=target['get']
    target['servers']=[{'url':'https://foreign.example/v2'}]
    with pytest.raises(UnsupportedEngine): _openapi('search','https://example.org/openapi.json',document,False)


def test_unadapted_reader_blocks_before_dns_even_with_legacy_builtin(tmp_path,monkeypatch):
    from knowledge_capture.providers import CaptureRouter,ProviderError
    settings=Settings(tmp_path)
    settings.save_providers({'services':{},'capabilities':{},'routing':{'default_reader':'builtin','capture':[{'hosts':['example.org'],'reader':'builtin'}]}})
    settings.save_engine('reader','api','https://example.org')
    config=settings.provider_configuration()
    assert config.data['routing']=={'default_reader':'engine_reader'}
    monkeypatch.setattr('knowledge_capture.providers._target',lambda _:pytest.fail('must reject before DNS'))
    monkeypatch.setattr('knowledge_capture.providers.capture_url',lambda *_:pytest.fail('must not use builtin'))
    with pytest.raises(ProviderError) as exc: CaptureRouter(config).capture('https://example.org/article',tmp_path)
    assert exc.value.code=='engine_not_ready'
    assert settings.public_view()['engines']['reader']['status']=='configured'


def test_new_reader_overrides_implicit_legacy_rules_but_explicit_takes_precedence(tmp_path):
    from knowledge_capture.providers import Configuration,CaptureRouter
    settings=Settings(tmp_path)
    legacy=Configuration({'services':{},'capabilities':{},'routing':{'capture':[{'hosts':['example.org'],'reader':'builtin'}],'default_reader':'builtin'}})
    settings.save_engine('reader','api','https://api.firecrawl.dev','secret')
    settings.discover_engine('reader')
    settings.save_providers(legacy.data)
    config=settings.provider_configuration()
    assert CaptureRouter(config)._reader('https://example.org/article')=='engine_reader'
    assert config.data['routing']=={'default_reader':'engine_reader'}
    explicit=settings.provider_configuration(legacy)
    assert explicit is legacy
    assert CaptureRouter(explicit)._reader('https://example.org/article')=='builtin'
    settings.save_engine('reader','api','https://unadapted.example')
    assert settings.provider_configuration().reader_error
    assert settings.provider_configuration(legacy) is legacy
    assert not hasattr(legacy,'reader_error')


def test_engine_credentials_public_status_matches_connector(tmp_path,monkeypatch):
    settings=Settings(tmp_path)
    settings.save_engine('search','api','https://api.search.brave.com','search-secret')
    settings.save_engine('reader','api','https://api.firecrawl.dev','reader-secret')
    settings.discover_engine('search')
    result=settings.discover_engine('reader')
    assert result['provider_credentials']['engine_reader']['token_configured'] is True
    assert result['provider_credentials']['engine_search']['headers_env']['X-Subscription-Token']['configured'] is True
    assert 'reader-secret' not in json.dumps(result) and 'search-secret' not in json.dumps(result)
    # Match Connector's explicit empty environment override instead of claiming
    # the protected stored value is active when the request will not use it.
    monkeypatch.setenv('KC_ENGINE_READER_KEY','')
    assert settings.public_view()['provider_credentials']['engine_reader']['token_configured'] is False
