"""Local protected settings. Public views never contain credential values."""
from __future__ import annotations

import copy
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .connectors import Connector, ConnectorError
from .llm import CloudClient, LLMError, _strict_json
from .providers import Configuration, set_path

ENV_MODEL = ('KC_LLM_BASE_URL', 'KC_LLM_MODEL', 'KC_LLM_API_KEY')
EMPTY_PROVIDERS = {'services': {}, 'capabilities': {}}
LIMIT = 256 * 1024


class SettingsError(ValueError):
    code = 'settings_invalid'


def _invalid():
    raise SettingsError('设置无效：请检查服务地址、字段映射及环境变量引用；不要填入原始凭据')


def _mapping(path, allow_empty=False):
    if (not isinstance(path, str) or len(path) > 500 or len(path.split('.')) > 20
            or (not path and not allow_empty) or (path and any(not p or any(ord(c) < 32 for c in p) for p in path.split('.')))):
        _invalid()


def _constants(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _invalid()
            compact = re.sub('[^a-z0-9]', '', key.lower())
            if any(term in compact for term in ('token', 'secret', 'password', 'authorization', 'apikey', 'cookie', 'credential')) or compact == 'headers':
                _invalid()
            _constants(item)
    elif isinstance(value, list):
        for item in value:
            _constants(item)


def validate_providers(config):
    """Validate every service and capability without contacting any endpoint."""
    try:
        encoded = json.dumps(config, allow_nan=False, ensure_ascii=False)
        if len(encoded.encode()) > LIMIT or not isinstance(config, dict) or set(config) - {'services', 'capabilities', 'routing'}:
            _invalid()
        services, capabilities = config['services'], config['capabilities']
        if not isinstance(services, dict) or not isinstance(capabilities, dict):
            _invalid()
        for name, service in services.items():
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name):
                _invalid()
            Connector(service)
            if 'allow_insecure_http' in service and type(service['allow_insecure_http']) is not bool:
                _invalid()
            if service['transport'] != 'mcp' and 'mcp_tool' in service:
                _invalid()
        outputs = {'results', 'title', 'url', 'description', 'html', 'markdown', 'final_url', 'images', 'status', 'segments', 'frames', 'duration', 'transcript_kind',
                   'segment_start', 'segment_end', 'segment_text', 'frame_timestamp', 'frame_url', 'frame_caption', 'frame_base64', 'frame_mime'}
        for name, capability in capabilities.items():
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name) or not isinstance(capability, dict):
                _invalid()
            if set(capability) - {'service', 'kind', 'constants', 'input_fields', 'output_fields', 'success_path', 'success_value', 'truncated_path', 'auto_content'}:
                _invalid()
            if capability.get('service') not in services or capability.get('kind') not in {None, 'video'}:
                _invalid()
            if 'auto_content' in capability and type(capability['auto_content']) is not bool:
                _invalid()
            constants = capability.get('constants', {})
            if not isinstance(constants, dict):
                _invalid()
            _constants(constants)
            inputs = capability.get('input_fields', {})
            output = capability.get('output_fields', {})
            expected = {'query', 'limit'} if name == 'search' else {'url'}
            if not isinstance(inputs, dict) or set(inputs) - expected or not isinstance(output, dict) or set(output) - outputs:
                _invalid()
            paths = [inputs.get(key, key) for key in sorted(expected)]
            probe = copy.deepcopy(constants)
            for path in paths:
                if path is None and capability.get('auto_content') is True:
                    continue
                _mapping(path)
                # Reject fixed payloads or another mapping shadowing a dynamic argument.
                current = probe
                for part in path.split('.')[:-1]:
                    if part in current and not isinstance(current[part], dict):
                        _invalid()
                    current = current.setdefault(part, {})
                if path.split('.')[-1] in current:
                    _invalid()
                set_path(probe, path, '__dynamic_value__')
            for path in output.values():
                _mapping(path, allow_empty=True)
            for key in ('success_path', 'truncated_path'):
                if key in capability:
                    _mapping(capability[key], allow_empty=True)
            if 'success_value' in capability and 'success_path' not in capability:
                _invalid()
        routing = config.get('routing', {})
        if not isinstance(routing, dict) or set(routing) - {'capture', 'default_reader'} or not isinstance(routing.get('capture', []), list):
            _invalid()
        readers = set(capabilities) - {'search'} | {'builtin'}
        if routing.get('default_reader', 'builtin') not in readers:
            _invalid()
        for rule in routing.get('capture', []):
            if not isinstance(rule, dict) or set(rule) != {'hosts', 'reader'} or rule['reader'] not in readers or not isinstance(rule['hosts'], list) or not rule['hosts']:
                _invalid()
            for host in rule['hosts']:
                if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(r'(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or '..' in host:
                    _invalid()
        return copy.deepcopy(config)
    except SettingsError:
        raise
    except (ConnectorError, ValueError, TypeError, KeyError, RecursionError):
        _invalid()


def _serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.root / '.settings.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise SettingsError('无法安全锁定设置文件') from None
        with os.fdopen(descriptor, 'r+') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise SettingsError('设置锁必须是普通文件')
            fcntl.flock(lock, fcntl.LOCK_EX)
            return method(self, *args, **kwargs)
    return wrapped


class Settings:
    def __init__(self, store_or_root):
        self.root = Path(store_or_root if isinstance(store_or_root, (str, os.PathLike)) else store_or_root.root).expanduser().resolve()
        self.path = self.root / '.settings.json'
        self.providers_path = self.root / 'providers.json'

    def _read(self, path):
        if not path.exists() and not path.is_symlink():
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    _invalid()
                raw = stream.read(LIMIT + 1)
            if len(raw) > LIMIT:
                _invalid()
            return _strict_json(raw.decode('utf-8'))
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise SettingsError('无法安全读取设置文件，请检查文件和权限') from None

    def _save(self, path, value):
        self.root.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SettingsError('设置路径必须是普通文件')
        staged = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.root, prefix='.settings-', delete=False) as stream:
                staged = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
        except (OSError, ValueError, TypeError):
            raise SettingsError('设置保存失败，原设置未被部分写入') from None
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    def _document(self):
        value = self._read(self.path)
        if value is None:
            return {'version': 1, 'provider_secrets': {}}
        if not isinstance(value, dict) or set(value) - {'version', 'model', 'provider_secrets', 'preferences', 'engines', 'retrieval'} or value.get('version') != 1:
            _invalid()
        preferences = value.get('preferences', {})
        if not isinstance(preferences, dict) or set(preferences) - {'auto_process'} or type(preferences.get('auto_process', False)) is not bool:
            _invalid()
        secrets = value.get('provider_secrets', {})
        if not isinstance(secrets, dict) or not all(isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) and isinstance(v, str) for k, v in secrets.items()):
            _invalid()
        engines = value.get('engines', {})
        if not isinstance(engines, dict) or set(engines) - {'search', 'reader'}:
            _invalid()
        for engine in engines.values():
            if not isinstance(engine, dict) or not all(isinstance(engine.get(k), str) for k in ('endpoint', 'transport', 'api_key', 'status', 'message')):
                _invalid()
            if engine['transport'] not in {'api', 'mcp'}:
                _invalid()
        return value

    def local_model(self):
        model = self._document().get('model')
        if model is None:
            return None
        if not isinstance(model, dict) or set(model) - {'base_url', 'model', 'api_key', 'timeout_seconds'} or not {'base_url', 'model', 'api_key'} <= set(model) or not all(isinstance(model[k], str) for k in ('base_url', 'model', 'api_key')):
            _invalid()
        try:
            CloudClient(model['base_url'], model['model'], model['api_key'] or 'validation-only', timeout_seconds=model.get('timeout_seconds', 180))
        except LLMError:
            _invalid()
        return model

    @_serialized
    def save_model(self, base_url, model, api_key='', *, clear_key=False, timeout_seconds=None):
        if not all(isinstance(v, str) for v in (base_url, model, api_key)) or type(clear_key) is not bool or (clear_key and api_key.strip()):
            _invalid()
        if any(len(v) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in (base_url, model, api_key)):
            _invalid()
        old = self.local_model() or {}
        timeout_seconds = old.get('timeout_seconds', 180) if timeout_seconds is None else timeout_seconds
        key = '' if clear_key else (api_key.strip() or old.get('api_key', ''))
        try:
            CloudClient(base_url, model, key or 'validation-only', timeout_seconds=timeout_seconds)
        except LLMError:
            _invalid()
        document = self._document()
        document['model'] = {'base_url': base_url.rstrip('/'), 'model': model.strip(), 'api_key': key, 'timeout_seconds': timeout_seconds}
        self._save(self.path, document)
        return self.public_view()

    @_serialized
    def save_preferences(self, auto_process):
        if type(auto_process) is not bool:
            _invalid()
        document = self._document()
        document['preferences'] = {'auto_process': auto_process}
        self._save(self.path, document)
        return self.public_view()

    def preferences(self):
        return {'auto_process': self._document().get('preferences', {}).get('auto_process', False)}

    @_serialized
    def save_providers(self, config):
        self._save(self.providers_path, validate_providers(config))
        return self.public_view()

    def provider_configuration(self, explicit=None):
        document = self._document()
        reader = document.get('engines', {}).get('reader')
        if explicit is not None:
            try:
                supplied = explicit if isinstance(explicit, Configuration) else Configuration.load(explicit)
                validate_providers(supplied.data)
            except Exception:
                raise SettingsError('指定的服务配置无效') from None
            return supplied
        value = self._read(self.providers_path)
        adapters = [(kind, entry) for kind, entry in document.get('engines', {}).items() if entry.get('status') == 'adapted' and entry.get('adapter')]
        if value is None and not adapters and not reader:
            return None
        config = copy.deepcopy(value or EMPTY_PROVIDERS)
        secrets = dict(document.get('provider_secrets', {}))
        for kind, entry in adapters:
            adapter = copy.deepcopy(entry['adapter'])
            variable = 'KC_ENGINE_' + kind.upper() + '_KEY'
            for service in adapter['services'].values():
                if service.get('token_env'):
                    service['token_env'] = variable
                service['headers_env'] = {name: variable for name in service.get('headers_env', {})}
            secrets[variable] = entry['api_key']
            config['services'].update(adapter['services'])
            config['capabilities'].update(adapter['capabilities'])
            if 'routing' in adapter:
                config['routing'] = adapter['routing']
        config = validate_providers(config)
        def factory(service):
            names = set(service.get('headers_env', {}).values()) | {service.get('token_env')}
            return Connector(service, credential_values={name: secrets[name] for name in names if name in secrets})
        result = Configuration(config, connector_factory=factory)
        if reader:
            # Saved reader intent wins over stale per-domain builtin/legacy rules.
            result.data['routing'] = {'default_reader': 'engine_reader'}
            if reader.get('status') != 'adapted' or not reader.get('adapter'):
                result.reader_error = '配置的反爬引擎尚未完成适配，未使用内置采集器替代。请先完成引擎识别。'
        return result

    @_serialized
    def save_provider_secret(self, variable, value='', clear=False):
        if not isinstance(variable, str) or not isinstance(value, str) or type(clear) is not bool or (clear and value.strip()):
            _invalid()
        if len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            _invalid()
        config = self._read(self.providers_path)
        if config is None:
            _invalid()
        config = validate_providers(config)
        names = set()
        for service in config['services'].values():
            names.update(service.get('headers_env', {}).values())
            if service.get('token_env'):
                names.add(service['token_env'])
        if variable not in names:
            _invalid()
        document = self._document()
        secrets = document.setdefault('provider_secrets', {})
        if clear:
            secrets.pop(variable, None)
        elif value.strip():
            secrets[variable] = value.strip()
        self._save(self.path, document)
        return self.public_view()

    def _engine_public(self, kind):
        entry = self._document().get('engines', {}).get(kind, {})
        return {'endpoint': entry.get('endpoint', ''), 'transport': entry.get('transport', 'api'),
                'key_configured': bool(entry.get('api_key')), 'status': entry.get('status', 'not_configured'),
                'message': entry.get('message', '尚未配置')}

    @_serialized
    def save_engine(self, kind, transport, endpoint, api_key=''):
        from .engines import service_config, _origin
        if kind not in {'search', 'reader'} or transport not in {'api', 'mcp'} or not all(isinstance(v, str) for v in (endpoint, api_key)):
            raise SettingsError('请选择有效的引擎和连接方式')
        if any(len(v) > 8192 or any(ord(c) <= 31 or ord(c) == 127 for c in v) for v in (endpoint, api_key)):
            raise SettingsError('服务地址或密钥格式无效')
        endpoint = endpoint.strip().rstrip('/')
        try:
            service_config(transport, endpoint)
        except ConnectorError:
            raise SettingsError('请填写有效的 HTTPS 服务地址；本机或私网 IP 可使用 HTTP') from None
        document = self._document()
        engines = document.setdefault('engines', {})
        old = engines.get(kind, {})
        same = old.get('transport') == transport and old.get('endpoint') and _origin(old['endpoint']) == _origin(endpoint)
        key = api_key.strip() or (old.get('api_key', '') if same else '')
        engines[kind] = {'endpoint': endpoint, 'transport': transport, 'api_key': key,
                         'status': 'configured', 'message': '已保存，等待自动识别'}
        self._save(self.path, document)
        return self.public_view()

    @_serialized
    def discover_engine(self, kind):
        from .engines import discover, UnsupportedEngine
        if kind not in {'search', 'reader'}:
            raise SettingsError('请选择有效的引擎')
        document = self._document()
        entry = document.get('engines', {}).get(kind)
        if not entry:
            raise SettingsError('请先保存引擎地址')
        entry.pop('adapter', None)
        try:
            adapter = discover(kind, entry['transport'], entry['endpoint'], entry['api_key'])
            entry['adapter'] = validate_providers(adapter)
            entry.update(status='adapted', message='已识别连接方式；实际内容质量将在采集时验证')
        except UnsupportedEngine as exc:
            entry.update(status='unsupported', message=str(exc))
        except ConnectorError as exc:
            entry.update(status='failed', message=str(exc))
        except (ValueError, KeyError, TypeError):
            entry.update(status='unsupported', message='服务规范无法自动识别，尚未启用该引擎')
        self._save(self.path, document)
        return self.public_view()

    def retrieval_config(self):
        value = self._document().get('retrieval', {})
        if not isinstance(value, dict) or set(value)-{'embedding','rerank'}:
            raise SettingsError('检索设置无效')
        for kind,spec in value.items():
            if not isinstance(spec,dict) or set(spec)-{'endpoint','model','api_key','enabled','protocol'} or type(spec.get('enabled')) is not bool:
                raise SettingsError('检索设置无效')
            if not all(isinstance(spec.get(key),str) for key in ('endpoint','model','api_key')) or spec.get('protocol') != ('openai_embeddings' if kind=='embedding' else 'cohere_rerank'):
                raise SettingsError('检索协议或配置无效')
        return copy.deepcopy(value)

    @_serialized
    def save_retrieval(self, kind, endpoint='', model='', api_key='', enabled=True, clear_key=False):
        from .engines import service_config, _origin
        if kind not in {'embedding','rerank'} or type(enabled) is not bool or type(clear_key) is not bool:
            raise SettingsError('检索设置无效')
        if not all(isinstance(v,str) and len(v)<=8192 and not any(ord(c)<32 or ord(c)==127 for c in v) for v in (endpoint,model,api_key)) or (clear_key and api_key.strip()):
            raise SettingsError('检索地址、模型或密钥格式无效')
        endpoint,model=endpoint.strip().rstrip('/'),model.strip()
        if enabled and (not endpoint or not model):
            raise SettingsError('启用可选检索时请填写完整接口地址和模型名称')
        if endpoint:
            try:
                service_config('api',endpoint)
            except ConnectorError:
                raise SettingsError('检索服务地址无效，请使用HTTPS或私网IP地址') from None
        document=self._document()
        config=document.setdefault('retrieval',{})
        old=config.get(kind,{})
        same=endpoint and old.get('endpoint') and _origin(endpoint)==_origin(old['endpoint'])
        key='' if clear_key else (api_key.strip() or (old.get('api_key','') if same else ''))
        config[kind]={'endpoint':endpoint,'model':model,'api_key':key,'enabled':enabled,
                      'protocol':'openai_embeddings' if kind=='embedding' else 'cohere_rerank'}
        self._save(self.path,document)
        return self.public_view()

    def _retrieval_public(self):
        config=self.retrieval_config()
        return {kind:{'endpoint':config.get(kind,{}).get('endpoint',''),
                      'model':config.get(kind,{}).get('model',''),
                      'enabled':config.get(kind,{}).get('enabled',False),
                      'key_configured':bool(config.get(kind,{}).get('api_key')),
                      'protocol':'openai_embeddings' if kind=='embedding' else 'cohere_rerank'}
                for kind in ('embedding','rerank')}

    def public_view(self):
        local = self.local_model()
        environment = any(name in os.environ for name in ENV_MODEL)
        values = dict(zip(('base_url', 'model', 'api_key'), (os.environ.get(n, '') for n in ENV_MODEL))) if environment else local
        values = values or {'base_url': '', 'model': '', 'api_key': ''}
        timeout_seconds = values.get('timeout_seconds', 180)
        if environment:
            raw_timeout = os.environ.get('KC_LLM_TIMEOUT_SECONDS', '180')
            timeout_seconds = int(raw_timeout) if raw_timeout.isascii() and raw_timeout.isdigit() else None
        valid = False
        try:
            CloudClient(values['base_url'], values['model'], 'validation-only', timeout_seconds=timeout_seconds)
            valid = True
        except LLMError:
            pass
        configuration = self.provider_configuration()
        providers = configuration.data if configuration else copy.deepcopy(EMPTY_PROVIDERS)
        document = self._document()
        secrets = dict(document.get('provider_secrets', {}))
        for kind, engine in document.get('engines', {}).items():
            if engine.get('status') == 'adapted' and engine.get('adapter'):
                secrets['KC_ENGINE_' + kind.upper() + '_KEY'] = engine.get('api_key', '')
        def configured(variable):
            return bool(os.environ.get(variable, secrets.get(variable, '')).strip())
        credentials = {}
        for name, spec in providers['services'].items():
            token_env = spec.get('token_env')
            credentials[name] = {'token_env': token_env, 'token_configured': configured(token_env) if token_env else False,
                                 'headers_env': {header: {'env': variable, 'configured': configured(variable)} for header, variable in spec.get('headers_env', {}).items()}}
        key_configured = bool(values['api_key'].strip())
        return {'model': {'base_url': values['base_url'] if valid else '', 'model': values['model'] if valid else '',
                          'key_configured': key_configured, 'configured': valid and key_configured, 'timeout_seconds': timeout_seconds,
                          'source': 'environment' if environment else ('local' if local else 'none'),
                          'environment_incomplete': environment and not (valid and key_configured)},
                'retrieval': self._retrieval_public(),
                'engines': {kind: self._engine_public(kind) for kind in ('search', 'reader')},
                'providers': providers, 'provider_credentials': credentials, 'preferences': self.preferences()}
