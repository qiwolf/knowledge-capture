"""Minimal engine settings and conservative, read-only protocol discovery."""
from urllib.parse import urlsplit, urljoin
import re
from .connectors import Connector, ConnectorError


class UnsupportedEngine(ValueError):
    pass


def service_config(transport, endpoint, key=False):
    config = {'transport': 'mcp' if transport == 'mcp' else 'http_json', 'endpoint': endpoint,
              'timeout_seconds': 30}
    if urlsplit(endpoint).scheme == 'http':
        config['allow_insecure_http'] = True
    if transport == 'mcp':
        config['mcp_tool'] = '__discovery__'
    if key:
        config['token_env'] = 'KC_ENGINE_KEY'
    Connector(config)
    return config


def _mapping(kind, schema):
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        return None
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return None
    if any(k in schema for k in ('$ref', 'oneOf', 'anyOf', 'allOf')):
        return None
    names = ('query', 'q', 'search_query') if kind == 'search' else ('url', 'target_url', 'web_url')
    matches = [name for name in names if isinstance(properties.get(name), dict) and properties[name].get('type') == 'string']
    if len(matches) != 1:
        return None
    primary = matches[0]
    # Constraints need an executable adapter, not a guessed request that violates schema.
    supported = {'type', 'title', 'description', 'default', 'examples'}
    if set(properties[primary]) - supported:
        return None
    inputs = {'query' if kind == 'search' else 'url': primary}
    consumed = {primary}
    if kind == 'search':
        limits = [name for name in ('limit', 'count', 'num', 'max_results') if isinstance(properties.get(name), dict) and properties[name].get('type') == 'integer']
        if len(limits) > 1 or any(set(properties[name]) - supported for name in limits):
            return None
        inputs['limit'] = limits[0] if limits else None
        consumed.update(limits)
    if set(required) - consumed:
        return None
    return inputs


def _spec(kind, service, inputs, constants=None):
    service = dict(service, response_format='auto')
    name = 'search' if kind == 'search' else 'engine_reader'
    result = {'services': {'engine_' + kind: service}, 'capabilities': {name: {
        'service': 'engine_' + kind, 'auto_content': True, 'input_fields': inputs}}}
    if constants:
        result['capabilities'][name]['constants'] = constants
    if kind == 'reader':
        result['routing'] = {'default_reader': name}
    return result


def _preset(kind, endpoint, key):
    parts = urlsplit(endpoint)
    host, path = parts.hostname, parts.path.rstrip('/')
    service = service_config('api', endpoint, key)
    if kind == 'search' and host == 'api.search.brave.com' and path in ('', '/res/v1/web/search'):
        service.update(endpoint='https://api.search.brave.com/res/v1/web/search', method='GET')
        service.pop('token_env', None)
        if key:
            service['headers_env'] = {'X-Subscription-Token': 'KC_ENGINE_KEY'}
        return _spec(kind, service, {'query': 'q', 'limit': 'count'})
    if kind == 'search' and host == 'google.serper.dev' and path in ('', '/search'):
        service.update(endpoint='https://google.serper.dev/search')
        service.pop('token_env', None)
        if key:
            service['headers_env'] = {'X-API-KEY': 'KC_ENGINE_KEY'}
        return _spec(kind, service, {'query': 'q', 'limit': 'num'})
    if kind == 'reader' and host == 'api.firecrawl.dev' and path in ('', '/v1', '/v1/scrape', '/v2', '/v2/scrape'):
        service['endpoint'] = 'https://api.firecrawl.dev' + ('/v1/scrape' if path.startswith('/v1') else '/v2/scrape')
        return _spec(kind, service, {'url': 'url'}, {'formats': ['markdown', 'html']})
    return None


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


def _openapi(kind, endpoint, document, key):
    if not isinstance(document, dict) or not str(document.get('openapi', '')).startswith('3.'):
        raise UnsupportedEngine('服务未提供可识别的 OpenAPI 3 规范。')
    servers = document.get('servers', [{'url': '/'}])
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict):
        raise UnsupportedEngine('服务规范包含多个调用地址，暂不能自动选择。')
    base = urljoin(endpoint, servers[0].get('url', '/'))
    if _origin(base) != _origin(endpoint) or '{' in base:
        raise UnsupportedEngine('服务规范的调用地址无法安全自动确认。')
    matches = []
    for path, operations in document.get('paths', {}).items():
        if not isinstance(path, str) or not path.startswith('/') or '{' in path or not isinstance(operations, dict):
            continue
        for method in ('get', 'post'):
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue
            # Path/operation overrides can change host and base path. Until supported,
            # never silently call the document-level server in their place.
            if 'servers' in operations or 'servers' in operation:
                continue
            hint = ' '.join(str(operation.get(k, '')) for k in ('operationId', 'summary')) + ' ' + path
            pattern = r'search|搜索|检索' if kind == 'search' else r'scrape|extract|crawl|fetch|read|读取|抓取|提取'
            if not re.search(pattern, hint, re.I) or re.search(r'delete|update|create|remove|write|batch|job|async', hint, re.I):
                continue
            params = operations.get('parameters', []) + operation.get('parameters', [])
            if method == 'get':
                if any(not isinstance(p, dict) or p.get('in') != 'query' for p in params):
                    continue
                schema = {'type': 'object', 'properties': {p.get('name'): p.get('schema') for p in params},
                          'required': [p.get('name') for p in params if p.get('required')]}
            else:
                if params:
                    continue
                schema = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {})
            inputs = _mapping(kind, schema)
            if inputs is None or '200' not in operation.get('responses', {}):
                continue
            security = operation.get('security', document.get('security', []))
            service = service_config('api', base.rstrip('/') + path, False)
            service['method'] = method.upper()
            if security:
                if not isinstance(security, list) or len(security) != 1 or len(security[0]) != 1:
                    continue
                security_name = next(iter(security[0]))
                auth = document.get('components', {}).get('securitySchemes', {}).get(security_name, {})
                if auth.get('type') == 'http' and auth.get('scheme', '').lower() == 'bearer':
                    if key:
                        service['token_env'] = 'KC_ENGINE_KEY'
                elif auth.get('type') == 'apiKey' and auth.get('in') == 'header':
                    if key:
                        service['headers_env'] = {auth.get('name'): 'KC_ENGINE_KEY'}
                else:
                    continue
            elif key:
                # No guessed authentication scheme for unknown APIs.
                continue
            Connector(service)
            matches.append(_spec(kind, service, inputs))
    if len(matches) != 1:
        raise UnsupportedEngine('未找到唯一且可直接调用的读取能力；服务规范需要明确输入与认证方式。')
    return matches[0]


def discover(kind, transport, endpoint, api_key='', connector_factory=Connector):
    """Return an internal adapter; never execute a search or capture request."""
    credentials = {'KC_ENGINE_KEY': api_key} if api_key else {}
    if transport == 'api':
        preset = _preset(kind, endpoint, bool(api_key))
        if preset:
            return preset
        # Fetch only JSON documentation, not arbitrary configured POST endpoints.
        parts = urlsplit(endpoint)
        docs = [endpoint] if parts.path.endswith(('.json', '/openapi', '/swagger')) else [endpoint.rstrip('/') + '/openapi.json', urljoin(endpoint, '/openapi.json')]
        error = None
        for address in dict.fromkeys(docs):
            config = service_config('api', address, False)
            config['method'] = 'GET'
            try:
                document = connector_factory(config).call({})
                return _openapi(kind, address, document, bool(api_key))
            except ConnectorError as exc:
                error = exc
            except UnsupportedEngine:
                raise
            except (AttributeError, KeyError, TypeError, ValueError):
                raise UnsupportedEngine('服务规范结构无法自动识别。') from None
        if error and error.code not in {'http_error', 'invalid_response'}:
            raise error
        raise UnsupportedEngine('未找到可自动识别的服务规范；当前地址尚不受支持。')
    config = service_config('mcp', endpoint, bool(api_key))
    tools = connector_factory(config, credential_values=credentials).list_tools()
    matches = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get('annotations'), dict) or tool['annotations'].get('readOnlyHint') is not True:
            continue
        inputs = _mapping(kind, tool.get('inputSchema'))
        hint = str(tool.get('name', '')) + ' ' + str(tool.get('description', ''))
        pattern = r'search|搜索|检索' if kind == 'search' else r'scrape|extract|crawl|fetch|read|读取|抓取|提取'
        if inputs is None or not re.search(pattern, hint, re.I):
            continue
        service = dict(config, mcp_tool=tool['name'], response_format='auto')
        matches.append(_spec(kind, service, inputs))
    if len(matches) != 1:
        raise UnsupportedEngine('未找到唯一且声明只读的工具；尚不能自动适配此服务。')
    return matches[0]
