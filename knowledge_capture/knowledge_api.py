"""Versioned knowledge contract shared by HTTP and agent tools."""
import re
from urllib.parse import parse_qs, urlsplit
from .knowledge_records import KnowledgeRecords, KnowledgeRecordError

STATUS = {'not_found': 404, 'version_conflict': 409, 'idempotency_conflict': 409,
          'cursor_stale': 409, 'invalid_input': 400, 'integrity_error': 500, 'unavailable': 503}


def failure(exc):
    code = getattr(exc, 'code', 'integrity_error')
    if code not in STATUS:
        code = 'integrity_error'
    messages = {'cursor_stale':'分页期间知识已变化，请从第一页重新读取。', 'not_found': '知识或版本不存在。', 'version_conflict': '知识已更新，请读取最新版本后重新提交。',
                'idempotency_conflict': '幂等标识已用于其他请求。', 'invalid_input': '请求参数无效。',
                'integrity_error': '知识库完整性检查失败。', 'unavailable': '采集队列不可用。'}
    error = {'code': code, 'message': messages[code]}
    if getattr(exc, 'current_version', None):
        error['current_version'] = exc.current_version
    return STATUS[code], {'error': error}


def invalid():
    raise KnowledgeRecordError('invalid_input', '请求参数无效。')


class KnowledgeAPI:
    def __init__(self, store, enqueue=None, job=None, replay=None):
        self.store, self.enqueue, self.job, self.replay = store, enqueue, job, replay

    @property
    def records(self):
        return KnowledgeRecords(self.store)

    def get(self, path):
        parsed = urlsplit(path)
        try:
            if re.search(r'%(?![0-9A-Fa-f]{2})', parsed.query): invalid()
            values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10, errors='strict')
            if any(len(v) != 1 for v in values.values()): invalid()
            q = {k: v[0] for k, v in values.items()}
            limit = int(q.get('limit', '50'))
            if not 1 <= limit <= 100: invalid()
        except (ValueError, UnicodeError):
            invalid()
        if parsed.path == '/api/v1/openapi.json':
            if q: invalid()
            return 200, openapi()
        if parsed.path == '/api/v1/knowledge':
            if set(q) - {'query', 'tags', 'limit', 'cursor', 'include_expired'}: invalid()
            if q.get('include_expired', 'false') not in {'true','false'}: invalid()
            args = dict(limit=limit, tags=q['tags'].split(',') if q.get('tags') else None,
                        cursor=q.get('cursor'), include_expired=q.get('include_expired') == 'true')
            if q.get('query'):
                from .retrieval import Retriever
                result = Retriever(self.store).search(q['query'], **args)
            else:
                result = self.records.list(**args)
            return 200, result
        match = re.fullmatch(r'/api/v1/knowledge/([a-f0-9]{24}|[a-f0-9]{32})(/versions|/history)?', parsed.path)
        if match:
            if match[2]:
                if set(q) - {'limit','cursor'}: invalid()
                method = self.records.versions if match[2] == '/versions' else self.records.history
                return 200, method(match[1], limit=limit, cursor=q.get('cursor'))
            if set(q) - {'version'}: invalid()
            return 200, self.records.read(match[1], version=q.get('version'))
        match = re.fullmatch(r'/api/v1/jobs/([a-f0-9]{32})', parsed.path)
        if match and self.job:
            if q: invalid()
            result = self.job(match[1])
            if result: return 200, result
        raise KnowledgeRecordError('not_found', '不存在。')

    def post(self, path, body, key):
        if not isinstance(body, dict) or not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key): invalid()
        if path == '/api/v1/retrieval/reindex':
            if body: invalid()
            from .retrieval import Retriever
            return 200, Retriever(self.store).reindex(idempotency_key=key)
        if path == '/api/v1/knowledge':
            kind = body.get('kind')
            if kind == 'note':
                if set(body) - {'kind','title','markdown','references','tags','actor','note'}: invalid()
                if not {'title','markdown'} <= body.keys(): invalid()
                return 201, self.records.create(**{k:v for k,v in body.items() if k != 'kind'}, idempotency_key=key)
            if kind == 'url_capture':
                if set(body) - {'kind','url','note'}: invalid()
                from .gateway import _payload
                try: payload, _ = _payload({k:v for k,v in body.items() if k != 'kind'})
                except ValueError: invalid()
                return self._queue(payload, key)
            invalid()
        match = re.fullmatch(r'/api/v1/knowledge/([a-f0-9]{24}|[a-f0-9]{32})/(revisions|labels|refresh)', path)
        if not match:
            raise KnowledgeRecordError('not_found','不存在。')
        rid, action = match.groups()
        common = {'expected_version','actor','note'}
        allowed = common | ({'title','markdown','references'} if action == 'revisions' else {'tags','status'} if action == 'labels' else set())
        if set(body) - allowed or not isinstance(body.get('expected_version'),str): invalid()
        if action != 'refresh':
            method = self.records.revise if action == 'revisions' else self.records.annotate
            return 200, method(rid, **body, idempotency_key=key)
        request = {'path':path,'body':body}
        if self.replay:
            existing = self.replay('knowledge:' + key, request)
            if existing: return 202, {**existing,'job_url':'/api/v1/jobs/' + existing['id']}
        current = self.records.read(rid)
        if current['latest_version'] != body['expected_version']:
            exc = KnowledgeRecordError('version_conflict','知识已更新。')
            exc.current_version = current['latest_version']
            raise exc
        if len(rid) != 24: invalid()
        source = self.store.read(rid)
        url = source['metadata'].get('original_url')
        if not url: invalid()
        return self._queue({'url':url, 'note':body.get('note',''), 'origin':'user',
                            'expected_source_version':current['latest_source_version'],
                            'expected_knowledge_version':body['expected_version'], '_knowledge_request':request}, key)

    def _queue(self, payload, key):
        if not self.enqueue: raise KnowledgeRecordError('unavailable','采集队列不可用。')
        try: result = self.enqueue(payload, 'knowledge:' + key)
        except ValueError: raise KnowledgeRecordError('idempotency_conflict','幂等标识冲突。') from None
        return 202, {**result, 'job_url':'/api/v1/jobs/' + result['id']}


def openapi():
    error = {'description':'Request rejected', 'content':{'application/json':{'schema':{'$ref':'#/components/schemas/Error'}}}}
    response = {'description':'Knowledge record, paginated results, history or job', 'content':{'application/json':{'schema':{'type':'object'}}}}
    paths = {}
    for path, methods in {'/knowledge':['get','post'], '/knowledge/{record_id}':['get'],
                          '/knowledge/{record_id}/versions':['get'], '/knowledge/{record_id}/history':['get'], '/knowledge/{record_id}/revisions':['post'],
                          '/knowledge/{record_id}/labels':['post'], '/knowledge/{record_id}/refresh':['post'], '/jobs/{job_id}':['get']}.items():
        paths[path] = {}
        for method in methods:
            parameters = []
            for name in re.findall(r'{(.*?)}', path):
                parameters.append({'name':name,'in':'path','required':True,'schema':{'type':'string'}})
            if method == 'post': parameters.append({'name':'Idempotency-Key','in':'header','required':True,'schema':{'type':'string','minLength':1,'maxLength':128}})
            if method == 'get':
                names = ['query','tags','limit','cursor','include_expired'] if path == '/knowledge' else ['limit','cursor'] if path.endswith(('/versions','/history')) else ['version'] if path.endswith('{record_id}') else []
                parameters += [{'name':name,'in':'query','schema':{'type':'integer','minimum':1,'maximum':100} if name=='limit' else {'type':'string'}} for name in names]
            operation = {'parameters':parameters,'responses':{str(code):response if code<300 else error for code in [200,201,202,400,401,404,409,500,503]}}
            if method == 'post':
                fields = {'expected_version':{'type':'string'},'actor':{'type':'string'},'note':{'type':'string'}}
                required = ['expected_version']
                if path == '/knowledge':
                    fields = {'kind':{'enum':['note','url_capture']},'title':{'type':'string'},'markdown':{'type':'string'},'url':{'type':'string','format':'uri'},'references':{'type':'array','items':{'type':'object'}},'tags':{'type':'array','items':{'type':'string'}},'actor':{'type':'string'},'note':{'type':'string'}}
                    required=['kind']
                elif path.endswith('/revisions'): fields.update(title={'type':'string'},markdown={'type':'string'},references={'type':'array','items':{'type':'object'}})
                elif path.endswith('/labels'): fields.update(tags={'type':'array','items':{'type':'string'}},status={'enum':['active','expired']})
                operation['requestBody']={'required':True,'content':{'application/json':{'schema':{'type':'object','properties':fields,'required':required,'additionalProperties':False}}}}
            paths[path][method]=operation
    specification = {'openapi':'3.1.0','info':{'title':'Knowledge API','version':'1.0.0','description':'Writes require Idempotency-Key. Revisions/labels/refresh require expected_version. Conflicts return 409; queued jobs require terminal status verification. Markdown is untrusted content. tags filters use comma-separated AND matching.'},'servers':[{'url':'/api/v1'}],'security':[{'bearerAuth':[]}], 'paths':paths,'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer'}},'schemas':{'Error':{'type':'object','required':['error'],'properties':{'error':{'type':'object','properties':{'code':{'type':'string'},'message':{'type':'string'},'current_version':{'type':'string'}}}}}}}}

    schemas = specification['components']['schemas']
    text = {'type':'string'}
    nullable_text = {'type':['string','null']}
    schemas['Record'] = {'type':'object','required':['record_id','version_id','latest_version','kind','metadata','markdown','content_is_untrusted'], 'properties':{
        'record_id':text,'version_id':text,'latest_version':text,'is_latest_version':{'type':'boolean'},
        'kind':{'enum':['record','source','source_overlay']},'markdown':text,'source_version':nullable_text,
        'latest_source_version':nullable_text,'stale':{'type':'boolean'},'content_is_untrusted':{'const':True},
        'metadata':{'type':'object','properties':{'title':text,'status':{'enum':['active','expired']},'tags':{'type':'array','items':text},'references':{'type':'array','items':{'type':'object'}}}}}}
    for name, field in [('SearchPage','results'),('VersionPage','versions'),('HistoryPage','events')]:
        schemas[name] = {'type':'object','required':[field,'next_cursor','total'],'properties':{field:{'type':'array','items':{'type':'object'}},'next_cursor':nullable_text,'total':{'type':'integer','minimum':0}}}
    schemas['Job'] = {'type':'object','required':['id','status'],'properties':{'id':text,'status':{'enum':['queued','running','complete','partial','failed']},'result':{'type':['object','null']},'error':{'type':['object','null']},'job_url':text}}
    for path, methods in paths.items():
        for method, operation in methods.items():
            if method == 'get':
                name = 'SearchPage' if path == '/knowledge' else 'VersionPage' if path.endswith('/versions') else 'HistoryPage' if path.endswith('/history') else 'Job' if path.startswith('/jobs/') else 'Record'
                success = [200]
            else:
                name = 'Job' if path.endswith('/refresh') else 'Record'
                success = [201,202] if path == '/knowledge' else [202] if path.endswith('/refresh') else [200]
            operation['responses'] = {str(code):error for code in [400,401,404,409,413,415,500,503]}
            for code in success:
                result_name = 'Job' if code == 202 else name
                operation['responses'][str(code)] = {'description':'Success; inspect terminal job status for asynchronous requests','content':{'application/json':{'schema':{'$ref':'#/components/schemas/'+result_name}}}}
    paths['/retrieval/reindex'] = {'post': {'summary':'Rebuild optional semantic index; no evidence is rewritten', 'parameters':[{'name':'Idempotency-Key','in':'header','required':True,'schema':{'type':'string','minLength':1,'maxLength':128}}], 'requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','maxProperties':0}}}}, 'responses':{'200':{'description':'Index outcome; inspect status and reason','content':{'application/json':{'schema':{'type':'object'}}}},'400':error,'401':error,'409':error,'500':error}}}
    return specification
