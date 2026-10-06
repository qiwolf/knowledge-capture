"""Optional hybrid retrieval over current source/maintained knowledge versions.

Scores route to evidence; they are not factual verification. Vector generations
bind endpoint/model, dimensions, source version and content digest. Failed optional
services report degradation and leave lexical retrieval available.
"""
from contextlib import closing
import hashlib
import json
import math
import re
import uuid

from .connectors import Connector, ConnectorError
from .engines import service_config
from .knowledge_records import KnowledgeRecords, KnowledgeRecordError
from .settings import Settings
from .store import now


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class RetrievalError(ValueError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


def _vector(value):
    if not isinstance(value,list) or not 1<=len(value)<=65536 or any(type(v) not in (float,int) or not math.isfinite(v) for v in value):
        raise RetrievalError('invalid_vectors','向量响应包含无效维度或数值')
    norm=math.sqrt(sum(v*v for v in value))
    if not math.isfinite(norm) or norm==0:
        raise RetrievalError('invalid_vectors','向量响应为空或无法归一化')
    return [v/norm for v in value]


class EmbeddingClient:
    def __init__(self,config): self.config=config
    def embed(self,texts):
        config=service_config('api',self.config['endpoint'],bool(self.config.get('api_key')))
        result=Connector(config,credential_values={'KC_ENGINE_KEY':self.config.get('api_key','')}).call(
            {'model':self.config['model'],'input':texts,'encoding_format':'float'})
        try:
            values=result['data']
            if not isinstance(values,list) or len(values)!=len(texts): raise ValueError()
            indexed={item['index']:item['embedding'] for item in values if type(item.get('index')) is int}
            if set(indexed)!=set(range(len(texts))): raise ValueError()
            return [_vector(indexed[index]) for index in range(len(texts))]
        except (KeyError,TypeError,ValueError):
            raise RetrievalError('invalid_vectors','嵌入接口没有返回完整且对应的向量') from None


class RerankClient:
    def __init__(self,config): self.config=config
    def rerank(self,query,documents):
        config=service_config('api',self.config['endpoint'],bool(self.config.get('api_key')))
        result=Connector(config,credential_values={'KC_ENGINE_KEY':self.config.get('api_key','')}).call(
            {'model':self.config['model'],'query':query,'documents':documents,'top_n':len(documents)})
        return result.get('results') if isinstance(result,dict) else None


class Retriever:
    def __init__(self,store,embedding_client=None,rerank_client=None):
        self.store=store
        self.records=KnowledgeRecords(store)
        self.config=Settings(store).retrieval_config()
        self.embedding=self.config.get('embedding',{})
        self.rerank=self.config.get('rerank',{})
        self.embedding_client=embedding_client or (EmbeddingClient(self.embedding) if self.embedding.get('enabled') else None)
        self.rerank_client=rerank_client or (RerankClient(self.rerank) if self.rerank.get('enabled') else None)
        self.identity={key:self.embedding.get(key) for key in ('endpoint','model','protocol')}
        self.identity_hash=_hash(self.identity)
        with closing(store._connect()) as db,db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS retrieval_runs (
                    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL,
                    identity_hash TEXT NOT NULL, identity_json TEXT NOT NULL, dimension INTEGER,
                    status TEXT NOT NULL, result_json TEXT, snapshot_hash TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS retrieval_vectors (
                    run_id TEXT NOT NULL, record_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL, vector_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,record_id,chunk_index)
                );
            ''')

    def _documents(self,include_expired=False,tags=None):
        rows=self.records._find('',include_expired,tags)
        documents=[]
        for row in rows:
            document=self.records.read(row['record_id'],row['version_id'])
            text=document['metadata']['title']+'\n'+document['markdown']
            documents.append({'row':row,'text':text,'content_hash':_hash(text)})
        return documents

    @staticmethod
    def _snapshot(documents):
        return _hash(sorted((item['row']['record_id'],item['row']['version_id'],item['content_hash']) for item in documents))

    def reindex(self,idempotency_key=None):
        if idempotency_key is not None and (not isinstance(idempotency_key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',idempotency_key)):
            raise KnowledgeRecordError('invalid_input','幂等标识无效')
        if not self.embedding.get('enabled'):
            return {'status':'disabled','message':'未启用嵌入服务，继续使用关键词检索','indexed_documents':0}
        request_hash=_hash(self.identity)
        run=uuid.uuid4().hex
        with closing(self.store._connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM retrieval_runs WHERE idempotency_key=?',(idempotency_key,)).fetchone() if idempotency_key else None
            if old:
                if old['request_hash']!=request_hash:
                    raise KnowledgeRecordError('idempotency_conflict','幂等标识已用于其他索引配置')
                return json.loads(old['result_json']) if old['result_json'] else {'run_id':old['id'],'status':old['status'],'message':'索引任务尚未完成；没有将排队视为成功'}
            db.execute('INSERT INTO retrieval_runs VALUES (?,?,?,?,?,NULL,\'running\',NULL,NULL,?,NULL)',
                       (run,idempotency_key,request_hash,self.identity_hash,_json(self.identity),now()))
        try:
            documents=self._documents()
            snapshot=self._snapshot(documents)
            chunks=[]
            for document in documents:
                # Every character is indexed, including tails; overlap helps sentences.
                for index,start in enumerate(range(0,len(document['text']),1440)):
                    chunks.append((document,index,document['text'][start:start+1600]))
            vectors=[];dimension=None
            for start in range(0,len(chunks),16):
                batch=chunks[start:start+16]
                embedded=self.embedding_client.embed([item[2] for item in batch])
                if not isinstance(embedded,list) or len(embedded)!=len(batch):
                    raise RetrievalError('invalid_vectors','嵌入服务漏返回索引内容')
                for (document,index,text),vector in zip(batch,embedded):
                    vector=_vector(vector)
                    dimension=dimension or len(vector)
                    if len(vector)!=dimension:
                        raise RetrievalError('dimension_mismatch','同次索引向量维度发生变化')
                    row=document['row']
                    vectors.append((run,row['record_id'],row['version_id'],document['content_hash'],index,text,_json(vector)))
            # Re-read before publishing; concurrent content edits require a fresh build.
            if self._snapshot(self._documents())!=snapshot:
                raise RetrievalError('source_changed','知识内容在建索引期间已变化，请重新构建')
            result={'run_id':run,'status':'complete','indexed_documents':len(documents),'indexed_chunks':len(chunks),
                    'dimension':dimension,'model_identity':self.identity,'snapshot_hash':snapshot,
                    'semantic_is_evidence':False}
            with closing(self.store._connect()) as db,db:
                db.execute('BEGIN IMMEDIATE')
                # Under the write lock, check effective heads against the snapshot.
                heads={r['id']:r['latest_version'] for r in db.execute('SELECT id,latest_version FROM sources')}
                for r in db.execute('SELECT id,latest_version,status FROM knowledge_records'):
                    if r['status']=='expired': heads.pop(r['id'],None)
                    else: heads[r['id']]=r['latest_version']
                expected={item['row']['record_id']:item['row']['version_id'] for item in documents}
                if heads!=expected: raise RetrievalError('source_changed','知识版本在索引提交前变化')
                db.executemany('INSERT INTO retrieval_vectors VALUES (?,?,?,?,?,?,?)',vectors)
                db.execute('UPDATE retrieval_runs SET status=\'complete\',dimension=?,result_json=?,snapshot_hash=?,completed_at=? WHERE id=?',
                           (dimension,_json(result),snapshot,now(),run))
            return result
        except Exception as exc:
            code=exc.code if isinstance(exc,(RetrievalError,ConnectorError,KnowledgeRecordError)) else 'index_failed'
            result={'run_id':run,'status':'failed','error_code':code,'message':'语义索引未完成；旧索引未替换，关键词检索仍可用'}
            with closing(self.store._connect()) as db,db:
                db.execute('UPDATE retrieval_runs SET status=\'failed\',result_json=?,completed_at=? WHERE id=?',(_json(result),now(),run))
            return result

    def search(self,query,limit=10,include_expired=False,tags=None,cursor=None):
        if not isinstance(query,str) or not query.strip() or len(query)>600 or any(ord(c)<32 for c in query):
            raise KnowledgeRecordError('invalid_input','搜索词无效')
        if type(limit) is not int or not 1<=limit<=100:
            raise KnowledgeRecordError('invalid_input','检索条数无效')
        documents=self._documents(include_expired,tags)
        live={item['row']['record_id']:item for item in documents}
        terms=query.casefold().split()
        lexical=[]
        for item in documents:
            text=(item['text']+' '+' '.join(item['row']['tags'])).casefold()
            if all(term in text for term in terms):
                score=sum(text.count(term) for term in terms)
                lexical.append((score,item['row']['record_id']))
        lexical.sort(reverse=True)
        orders=[[rid for _,rid in lexical]]
        snippets={}
        reasons=[];index_state='disabled';mode='lexical'
        if self.embedding.get('enabled'):
            with closing(self.store._connect()) as db:
                generation=db.execute('SELECT * FROM retrieval_runs WHERE identity_hash=? AND status=\'complete\' ORDER BY completed_at DESC,rowid DESC LIMIT 1',(self.identity_hash,)).fetchone()
                stored=list(db.execute('SELECT * FROM retrieval_vectors WHERE run_id=?',(generation['id'],))) if generation else []
            if not generation:
                index_state='missing';reasons.append('index_missing')
            elif not generation['dimension']:
                index_state='empty';reasons.append('index_empty')
            else:
                try:
                    result=self.embedding_client.embed([query])
                    if not isinstance(result,list) or len(result)!=1: raise RetrievalError('invalid_vectors','查询向量无效')
                    vector=_vector(result[0])
                    if len(vector)!=generation['dimension']: raise RetrievalError('dimension_mismatch','查询维度与索引不同')
                    semantic={};valid=set()
                    for row in stored:
                        item=live.get(row['record_id'])
                        if item is None or row['version_id']!=item['row']['version_id'] or row['content_hash']!=item['content_hash']: continue
                        indexed=_vector(json.loads(row['vector_json']))
                        if len(indexed)!=len(vector): raise RetrievalError('index_invalid','索引向量维度无效')
                        valid.add(row['record_id'])
                        score=sum(a*b for a,b in zip(vector,indexed))
                        if row['record_id'] not in semantic or score>semantic[row['record_id']]:
                            semantic[row['record_id']]=score;snippets[row['record_id']]=row['text'][:600]
                    orders.append(sorted(semantic,key=lambda rid:(semantic[rid],rid),reverse=True)[:100])
                    mode='hybrid';index_state='ready'
                    missing=set(live)-valid
                    if missing: index_state='stale';reasons.append('index_stale')
                except Exception as exc:
                    index_state='degraded'
                    reasons.append(exc.code if isinstance(exc,(RetrievalError,ConnectorError)) else 'embedding_failed')
        bounded=bool(self.embedding.get('enabled') or self.rerank.get('enabled'))
        fused={}
        for order in orders:
            for rank,rid in enumerate(order[:100] if bounded else order): fused[rid]=fused.get(rid,0)+1/(60+rank+1)
        candidates=sorted(fused,key=lambda rid:(fused[rid],rid),reverse=True)
        if bounded: candidates=candidates[:100]
        if self.rerank.get('enabled') and candidates:
            try:
                docs=[live[rid]['row']['title']+'\n'+snippets.get(rid,live[rid]['row']['excerpt']) for rid in candidates]
                scores=self.rerank_client.rerank(query,docs)
                if not isinstance(scores,list) or len(scores)!=len(candidates): raise RetrievalError('rerank_invalid','重排响应不完整')
                selected={}
                for item in scores:
                    if not isinstance(item,dict) or type(item.get('index')) is not int or not 0<=item['index']<len(candidates) or item['index'] in selected or type(item.get('relevance_score')) not in (int,float) or not math.isfinite(item['relevance_score']):
                        raise RetrievalError('rerank_invalid','重排候选不在本次请求内')
                    selected[item['index']]=item['relevance_score']
                candidates=[candidates[i] for i in sorted(selected,key=lambda i:selected[i],reverse=True)]
                mode+=' + rerank'
            except Exception as exc:
                reasons.append(exc.code if isinstance(exc,(RetrievalError,ConnectorError)) else 'rerank_failed')
        results=[]
        for rid in candidates:
            row=dict(live[rid]['row'])
            if rid in snippets: row['excerpt']=snippets[rid]
            row['retrieval_score']=fused[rid]
            row['retrieval_score_kind']='reciprocal_rank_fusion'
            row['similarity_is_truth']=False
            results.append(row)
        page,next_cursor=KnowledgeRecords._page(results,limit,cursor)
        return {'results':page,'next_cursor':next_cursor,'total':len(results),
                'retrieval':{'mode':mode,'degraded':bool(reasons),'reasons':list(dict.fromkeys(reasons)),
                             'index_state':index_state,'semantic_is_evidence':False,'candidate_limit':100 if bounded else None,
                             'candidate_truncated':bounded and (len(lexical)>100 or len(live)>100)}}
