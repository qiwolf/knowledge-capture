"""Human/agent maintained Markdown alongside immutable captured sources.

Every write adds an immutable version and audit event in the existing library.
Captured source files are references only and are never modified here.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import urlsplit

from .store import now, canonical_url


class KnowledgeRecordError(ValueError):
    def __init__(self, code, message, current_version=None):
        self.code = code
        self.current_version = current_version
        super().__init__(message)


def _fail(code='invalid_input', message='知识维护参数无效'):
    raise KnowledgeRecordError(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _id(value, length=None):
    pattern = '[a-f0-9]{'+str(length)+'}' if length else '(?:[a-f0-9]{24}|[a-f0-9]{32})'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        _fail()
    return value


def _line(value, maximum=500, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or any(ord(c)<32 or ord(c)==127 for c in value):
        _fail()
    return value.strip()


def _tags(values):
    if not isinstance(values, list) or len(values)>50:
        _fail()
    return sorted(set(_line(value,80) for value in values))


class KnowledgeRecords:
    def __init__(self, store, *, initialize=True):
        self.store, self.root = store, store.root
        if not initialize:
            return
        with closing(store._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge_record_idempotency (
                    key TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_records (
                    id TEXT PRIMARY KEY, latest_version TEXT NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_record_versions (
                    record_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    parent_version TEXT, path TEXT NOT NULL,
                    markdown_sha256 TEXT NOT NULL, metadata_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(record_id,version_id)
                );
                CREATE TABLE IF NOT EXISTS knowledge_record_events (
                    id TEXT PRIMARY KEY, record_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    event TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            ''')

    def _safe(self, path, *, exists=False):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_relative_to(self.root) or '..' in path.parts:
            _fail('integrity_error','知识记录路径无效')
        for part in (path, *path.parents):
            if part.is_symlink():
                _fail('integrity_error','知识记录路径不允许符号链接')
            if part == self.root:
                break
        if exists and not path.is_file():
            _fail('integrity_error','知识记录历史文件缺失')
        return path

    def _references(self, values):
        if not isinstance(values,list) or len(values)>100:
            _fail()
        result=[]
        for ref in values:
            if not isinstance(ref,dict):
                _fail()
            if 'source_id' in ref:
                if set(ref) != {'source_id','version_id'}:
                    _fail()
                sid,version = _id(ref['source_id'],24),_id(ref['version_id'],24)
                with closing(self.store._connect()) as db:
                    row=db.execute('SELECT path FROM versions WHERE source_id=? AND version_id=?',(sid,version)).fetchone()
                if row is None:
                    _fail('not_found','引用的原始来源版本不存在')
                path=self._safe(Path(row['path'])/'content.md',exists=True)
                result.append({'source_id':sid,'version_id':version,'markdown_sha256':_hash(path.read_text(encoding='utf-8'))})
            else:
                if set(ref)-{'url','title'} or 'url' not in ref:
                    _fail()
                value=_line(ref['url'],2048)
                try:
                    url=canonical_url(value)
                    urlsplit(url).port
                except ValueError:
                    _fail()
                result.append({'url':url,'title':_line(ref.get('title',''),500,empty=True),
                               'evidence_status':'external_reference_not_captured'})
        return list({_json(ref):ref for ref in result}.values())

    def _read(self, db, record_id, version=None):
        _id(record_id)
        if version is not None:
            _id(version)
        record=db.execute('SELECT * FROM knowledge_records WHERE id=?',(record_id,)).fetchone()
        if record is None or (version is not None and len(version)==24 and len(record_id)==24):
            return self._source(db,record_id,version)
        row=db.execute('SELECT * FROM knowledge_record_versions WHERE record_id=? AND version_id=?',
                       (record_id,version or record['latest_version'])).fetchone()
        if row is None:
            _fail('not_found','指定知识历史版本不存在')
        markdown=self._safe(Path(row['path'])/'content.md',exists=True).read_text(encoding='utf-8')
        metadata_text=self._safe(Path(row['path'])/'metadata.json',exists=True).read_text(encoding='utf-8')
        if _hash(markdown)!=row['markdown_sha256'] or _hash(metadata_text)!=row['metadata_sha256']:
            _fail('integrity_error','知识历史版本校验失败')
        try:
            metadata=json.loads(metadata_text)
        except ValueError:
            _fail('integrity_error','知识历史元数据无效')
        if metadata.get('record_id')!=record_id or metadata.get('version_id')!=row['version_id']:
            _fail('integrity_error','知识历史版本身份不一致')
        source = db.execute('SELECT latest_version FROM sources WHERE id=?',(record_id,)).fetchone() if len(record_id)==24 else None
        source_version = metadata.get('source_version')
        return {'record_id':record_id,'version_id':row['version_id'],'latest_version':record['latest_version'],
                'is_latest_version':row['version_id']==record['latest_version'], 'kind':'source_overlay' if source else 'record',
                'source_version':source_version,'latest_source_version':source['latest_version'] if source else None,
                'stale':bool(source and source['latest_version']!=source_version),
                'metadata':metadata,'markdown':markdown,'content_is_untrusted':True}

    def _source(self,db,record_id,version=None):
        source=db.execute('SELECT * FROM sources WHERE id=?',(record_id,)).fetchone()
        if source is None:
            _fail('not_found','知识记录不存在')
        version=version or source['latest_version']
        row=db.execute('SELECT * FROM versions WHERE source_id=? AND version_id=?',(record_id,version)).fetchone()
        if row is None:
            _fail('not_found','指定原文版本不存在')
        full=self._safe(Path(row['path'])/'content.md',exists=True).read_text(encoding='utf-8')
        source_metadata=json.loads(self._safe(Path(row['path'])/'metadata.json',exists=True).read_text(encoding='utf-8'))
        overlay=db.execute('SELECT latest_version FROM knowledge_records WHERE id=?',(record_id,)).fetchone()
        head=overlay['latest_version'] if overlay else source['latest_version']
        metadata={'record_id':record_id,'version_id':version,'title':source_metadata['title'],
                  'status':'active','capture_status':row['status'],'tags':[],
                  'references':[{'source_id':record_id,'version_id':version,'markdown_sha256':_hash(full)}],
                  'source_version':version,'origin':'captured_source','evidence_status':'captured_original',
                  'created_at':row['created_at'],'original_url':source['url']}
        return {'record_id':record_id,'version_id':version,'latest_version':head,
                'is_latest_version':version==head,'kind':'source','metadata':metadata,
                'markdown':full.split('---\n\n',1)[-1], 'content_is_untrusted':True,
                'source_version':version,'latest_source_version':source['latest_version'],
                'stale':version!=source['latest_version']}

    def _mutate(self,operation,arguments,idempotency_key,callback):
        if idempotency_key is not None:
            idempotency_key=_line(idempotency_key,200)
        _line(arguments.get('actor',''),120)
        _line(arguments.get('note',''),2000,empty=True)
        try:
            request_hash=_hash(_json({'operation':operation,'arguments':arguments}))
        except (ValueError,TypeError):
            _fail()
        with closing(self.store._connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            previous = (db.execute('SELECT * FROM knowledge_record_idempotency WHERE key=?',(idempotency_key,)).fetchone()
                        if idempotency_key is not None else None)
            if previous:
                if previous['request_sha256']!=request_hash:
                    _fail('idempotency_conflict','同一幂等标识已用于不同请求')
                result=json.loads(previous['response_json'])
                # A retry never conceals loss/corruption of the committed version.
                self._read(db,result['record_id'],result['version_id'])
            else:
                result=callback(db)
                if idempotency_key is not None:
                    db.execute('INSERT INTO knowledge_record_idempotency VALUES (?,?,?,?)',
                               (idempotency_key,request_hash,_json(result),now()))
        from .vault_export import sync_after_commit
        sync_after_commit(self.store)
        return result

    def read(self, record_id, version=None):
        with closing(self.store._connect()) as db:
            return self._read(db,record_id,version)

    def _write(self, db, record_id, parent, title, markdown, references, tags, status, actor, note, events, source_version=None):
        title=_line(title)
        actor=_line(actor,120)
        note=_line(note,2000,empty=True)
        if not isinstance(markdown,str) or not markdown.strip() or len(markdown.encode('utf-8'))>2_000_000 or '\x00' in markdown:
            _fail()
        if status not in {'active','expired'}:
            _fail()
        tags=_tags(tags)
        version=uuid.uuid4().hex
        timestamp=now()
        if source_version:
            base_ref=self._references([{'source_id':record_id,'version_id':source_version}])[0]
            prior=next((ref for ref in references if ref.get('source_id')==record_id and ref.get('version_id')==source_version),None)
            if prior and prior.get('markdown_sha256')!=base_ref['markdown_sha256']:
                _fail('integrity_error','绑定的原始来源版本内容发生变化')
            references=[base_ref]+[ref for ref in references if not (ref.get('source_id')==record_id and ref.get('version_id')==source_version)]
        metadata={'schema_version':1,'record_id':record_id,'version_id':version,'parent_version':parent,
                  'title':title,'tags':tags,'status':status,'references':references,
                  'actor':actor,'note':note,'created_at':timestamp,'origin':'maintained_knowledge',
                  'evidence_status':'authored_record_not_original_source','source_version':source_version}
        encoded=_json(metadata)+'\n'
        relative=Path('records')/record_id/'versions'/version
        destination=self._safe(relative)
        self._safe(destination.parent).mkdir(parents=True,exist_ok=True,mode=0o700)
        # Publish a new random version, never overwrite an existing document.
        with tempfile.TemporaryDirectory(prefix='.record-',dir=destination.parent) as temporary:
            staged=Path(temporary)/'version'
            staged.mkdir(mode=0o700)
            for name,text in [('content.md',markdown),('metadata.json',encoded)]:
                with (staged/name).open('x',encoding='utf-8') as file:
                    os.fchmod(file.fileno(),0o600)
                    file.write(text)
                    file.flush()
                    os.fsync(file.fileno())
            if destination.exists():
                _fail('integrity_error','知识版本目标已存在')
            staged.rename(destination)
        db.execute('INSERT INTO knowledge_record_versions VALUES (?,?,?,?,?,?,?)',
                   (record_id,version,parent,str(relative),_hash(markdown),_hash(encoded),timestamp))
        db.execute('''INSERT INTO knowledge_records VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET latest_version=excluded.latest_version,title=excluded.title,
            status=excluded.status,updated_at=excluded.updated_at''',(record_id,version,title,status,timestamp,timestamp))
        for event in events:
            db.execute('INSERT INTO knowledge_record_events VALUES (?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex,record_id,version,event,actor,note,timestamp))
        return self._read(db,record_id,version)

    def create(self, title, markdown, references=None, tags=None, actor='user', note='', idempotency_key=None):
        arguments=locals().copy(); arguments.pop('self'); arguments.pop('idempotency_key')
        def write(db):
            refs=self._references([] if references is None else references)
            return self._write(db,uuid.uuid4().hex,None,title,markdown,refs,[] if tags is None else tags,
                               'active',actor,note,['created'])
        return self._mutate('create',arguments,idempotency_key,write)

    def _current(self,db,record_id,expected_version):
        _id(expected_version)
        current=self._read(db,record_id)
        if current['version_id']!=expected_version:
            raise KnowledgeRecordError('version_conflict','知识已被其他人修订，请读取最新版本后合并',current['version_id'])
        return current

    def revise(self, record_id, expected_version, title=None, markdown=None, references=None, actor='agent', note='', idempotency_key=None):
        arguments=locals().copy(); arguments.pop('self'); arguments.pop('idempotency_key')
        if title is None and markdown is None and references is None:
            _fail('invalid_input','修订必须提供标题、正文或引用的变更')
        def write(db):
            current=self._current(db,record_id,expected_version)
            old=current['metadata']
            refs=self._references(references) if references is not None else old['references']
            return self._write(db,record_id,expected_version,title if title is not None else old['title'],
                               markdown if markdown is not None else current['markdown'],refs,
                               old['tags'],old['status'],actor,note,['revised'],old.get('source_version'))
        return self._mutate('revise',arguments,idempotency_key,write)

    def annotate(self, record_id, expected_version, tags=None, status=None, actor='agent', note='', idempotency_key=None):
        arguments=locals().copy(); arguments.pop('self'); arguments.pop('idempotency_key')
        if tags is None and status is None:
            _fail('invalid_input','请提供标签或知识状态')
        if status is not None and status not in {'active','expired'}:
            _fail()
        def write(db):
            current=self._current(db,record_id,expected_version)
            old=current['metadata']
            selected_tags=_tags(tags) if tags is not None else old['tags']
            events=[]
            if selected_tags!=old['tags']:
                events.append('tagged')
            if status is not None and status!=old['status']:
                events.append('expired' if status=='expired' else 'reactivated')
            if not events:
                return current
            return self._write(db,record_id,expected_version,old['title'],current['markdown'],old['references'],
                               selected_tags,status if status is not None else old['status'],actor,note,events,old.get('source_version'))
        return self._mutate('annotate',arguments,idempotency_key,write)

    @staticmethod
    def _page(values,limit,cursor):
        if type(limit) is not int or not 1<=limit<=100:
            _fail()
        snapshot=_hash(_json(values))
        if cursor is None:
            offset=0
        elif isinstance(cursor,str) and re.fullmatch(r'[0-9]{1,12}\.[a-f0-9]{64}',cursor):
            offset,expected=cursor.split('.')
            offset=int(offset)
            if expected!=snapshot:
                _fail('cursor_stale','结果已变化，请从首页重新读取以避免遗漏')
        else:
            _fail()
        if offset>len(values):
            _fail('invalid_input','分页位置超过当前结果范围')
        end=min(offset+limit,len(values))
        return values[offset:end],str(end)+'.'+snapshot if end<len(values) else None

    def history(self, record_id, limit=50, cursor=None):
        _id(record_id)
        with closing(self.store._connect()) as db:
            self._read(db,record_id)
            events=[dict(row) for row in db.execute('SELECT * FROM knowledge_record_events WHERE record_id=? ORDER BY created_at,rowid',(record_id,))]
            if len(record_id)==24:
                events += [{'record_id':record_id,'version_id':row['version_id'],'event':'captured',
                            'actor':'capture','note':'','created_at':row['created_at']} for row in db.execute('SELECT * FROM versions WHERE source_id=?',(record_id,))]
        events.sort(key=lambda entry:(entry['created_at'],entry.get('id','')))
        page,cursor=self._page(events,limit,cursor)
        return {'events':page,'next_cursor':cursor,'total':len(events)}

    def versions(self, record_id, limit=50, cursor=None):
        with closing(self.store._connect()) as db:
            self._read(db,record_id)
            versions=[{'version_id':row['version_id'],'parent_version':row['parent_version'],'created_at':row['created_at'],'kind':'record'} for row in db.execute('SELECT * FROM knowledge_record_versions WHERE record_id=?',(record_id,))]
            if len(record_id)==24:
                versions += [{'version_id':row['version_id'],'parent_version':None,'created_at':row['created_at'],'kind':'source'} for row in db.execute('SELECT * FROM versions WHERE source_id=?',(record_id,))]
        versions.sort(key=lambda item:(item['created_at'],item['version_id']),reverse=True)
        page,cursor=self._page(versions,limit,cursor)
        return {'versions':page,'next_cursor':cursor,'total':len(versions)}

    def _find(self,query,include_expired,tags):
        if type(include_expired) is not bool:
            _fail()
        selected_tags=_tags([] if tags is None else tags)
        terms=query.casefold().split() if query else []
        matches=[]
        with closing(self.store._connect()) as db:
            # One effective head per ID: expired overlays cannot fall back to sources.
            heads={row['id']:row['updated_at'] for row in db.execute('SELECT id,updated_at FROM sources')}
            heads.update({row['id']:row['updated_at'] for row in db.execute('SELECT id,updated_at FROM knowledge_records')})
            for record_id,updated_at in heads.items():
                document=self._read(db,record_id)
                metadata=document['metadata']
                if metadata['status']=='expired' and not include_expired:
                    continue
                if not set(selected_tags)<=set(metadata['tags']):
                    continue
                body=document['markdown']
                haystack=(metadata['title']+'\n'+body+'\n'+' '.join(metadata['tags'])).casefold()
                if not all(term in haystack for term in terms):
                    continue
                at=next((body.casefold().find(term) for term in terms if term in body.casefold()),0)
                matches.append({'kind':document['kind'],'record_id':record_id,'version_id':document['version_id'],
                                'title':metadata['title'],'status':metadata['status'],'tags':metadata['tags'],
                                'excerpt':body[max(0,at-60):at+240],'excerpt_is_partial':True,
                                'updated_at':updated_at,'references':metadata['references'],
                                'source_version':document['source_version'],'latest_source_version':document['latest_source_version'],
                                'stale':document['stale']})
        matches.sort(key=lambda item:(item['updated_at'],item['record_id']),reverse=True)
        return matches

    def search(self, query, limit=10, include_expired=False, tags=None, cursor=None):
        query=_line(query,600)
        values=self._find(query,include_expired,tags)
        page,cursor=self._page(values,limit,cursor)
        return {'results':page,'next_cursor':cursor,'total':len(values)}

    def list(self, limit=50, include_expired=False, tags=None, cursor=None):
        values=self._find('',include_expired,tags)
        page,cursor=self._page(values,limit,cursor)
        return {'results':page,'next_cursor':cursor,'total':len(values)}

    def audit(self):
        """Report broken committed versions and uncommitted crash leftovers."""
        invalid,orphans=[],[]
        with closing(self.store._connect()) as db:
            rows=list(db.execute('SELECT record_id,version_id FROM knowledge_record_versions'))
            registered={(row['record_id'],row['version_id']) for row in rows}
            for row in rows:
                try:
                    self._read(db,row['record_id'],row['version_id'])
                except (KnowledgeRecordError,OSError,ValueError):
                    invalid.append({'record_id':row['record_id'],'version_id':row['version_id']})
        folder=self._safe('records')
        if folder.exists():
            for record in folder.iterdir():
                if not re.fullmatch(r'(?:[a-f0-9]{24}|[a-f0-9]{32})',record.name):
                    continue
                versions=self._safe(record/'versions')
                if versions.exists():
                    for version in versions.iterdir():
                        if re.fullmatch('[a-f0-9]{32}',version.name) and (record.name,version.name) not in registered:
                            orphans.append({'record_id':record.name,'version_id':version.name})
        return {'ok':not invalid and not orphans,'invalid_versions':invalid,'orphan_versions':orphans}
