"""A linked Obsidian reading mirror of the authoritative local library.

No source/analysis/wiki/record is edited here. Managed files are refreshed only
while their hashes still match the previous export. User edits remain in place;
a complete alternate vault is emitted for review when conflicts occur.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import quote, unquote, urlsplit

from .processing import body_of, md_text
from .store import now

MANIFEST = '.knowledge-vault-manifest.json'
MANAGED_BY = 'knowledge-capture-vault/1'


class VaultExportError(ValueError):
    code = 'vault_export_failed'


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _safe(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise VaultExportError('知识库镜像路径不合法')
    target = root / relative
    for path in (target, *target.parents):
        if path.is_symlink():
            raise VaultExportError('知识库镜像不读写符号链接')
        if path == root:
            break
    return target


def _source_file(root, relative):
    path = _safe(root, relative)
    if not path.is_file():
        raise VaultExportError('历史资料或图片文件缺失：' + str(relative))
    return path.read_bytes()


def _body(markdown):
    if markdown.startswith('---\n') and '\n---\n' in markdown[4:]:
        return markdown.split('\n---\n', 1)[1].lstrip('\n')
    return markdown


def _front(text):
    if not text.startswith('---\n') or '\n---\n' not in text[4:]:
        return {}
    output = {}
    for line in text.split('\n---\n', 1)[0].splitlines()[1:]:
        key, sep, value = line.partition(': ')
        if not sep:
            return {}
        try:
            output[key] = json.loads(value)
        except ValueError:
            return {}
    return output


def _markdown(body, metadata):
    return ('---\n' + '\n'.join(key + ': ' + _json(value) for key, value in metadata.items()) +
            '\n---\n\n' + _body(body).rstrip() + '\n').encode('utf-8')


def _link(label, source, destination):
    relative = os.path.relpath(destination, Path(source).parent).replace(os.sep, '/')
    return '[' + md_text(str(label)) + '](' + quote(relative, safe='/._-') + ')'


def _metadata(kind, *, source_id=None, version_id=None, ai=False, stale=False,
              expired=False, withdrawn=False, revised=False, title='', tags=(), **extra):
    labels = {'source':'原始资料', 'source_index':'来源索引', 'analysis':'AI整理',
              'evidence':'证据', 'wiki':'主题Wiki', 'record':'知识记录', 'index':'目录'}
    status_tags = ['撤回'] if withdrawn else ['过期'] if expired else ['历史版本'] if stale else []
    return {'managed_by':MANAGED_BY, 'kind':kind, 'title':title,
            'tags':list(dict.fromkeys([labels.get(kind,kind), 'AI派生' if ai else '原文或人工资料',
                                     '修订' if revised else '新增', *status_tags, *tags])),
            'source_id':source_id, 'version_id':version_id, 'ai_derived':ai,
            'stale':bool(stale), 'expired':bool(expired), 'withdrawn':bool(withdrawn),
            '新增':not revised, '修订':bool(revised), '过期':bool(expired or stale),
            'authoritative':False, **extra}


class _Builder:
    def __init__(self, store):
        self.store, self.root = store, store.root
        self.files, self.sources, self.source_pages, self.analysis_pages, self.wiki_pages, self.record_pages = {}, {}, {}, [], [], []
        self.backlinks = {}
        self.source_metadata = {}
        with closing(store._connect()) as db:
            db.execute('BEGIN')
            tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            def rows(table):
                return [dict(r) for r in db.execute('SELECT * FROM '+table)] if table in tables else []
            self.source_rows = rows('sources')
            self.version_rows = rows('versions')
            self.analysis_rows = rows('analysis_runs')
            self.wiki_rows = rows('wiki_versions')
            self.wiki_heads = {r['topic_id']:r for r in rows('wiki_pages')}
            self.record_rows = rows('knowledge_records')
            self.record_versions = rows('knowledge_record_versions')
        self.source_heads = {r['id']:r for r in self.source_rows}
        self.record_heads = {r['id']:r for r in self.record_rows}

    def add(self, path, body, metadata, links=()):
        if links:
            body = _body(body).rstrip() + '\n\n## 关联资料\n\n' + '\n'.join('- '+_link(label,path,target) for label,target in links)
        self.files[path] = _markdown(body, metadata)

    def assets(self, folder, target, metadata):
        for asset in metadata.get('assets', []):
            if asset.get('status') != 'complete':
                continue
            rel = asset['relative_path']
            if len(Path(rel).parts) != 2 or Path(rel).parts[0] != 'assets':
                raise VaultExportError('原文图片路径不合法')
            binary = _source_file(self.root, Path(folder)/rel)
            if _hash(binary) != asset['sha256']:
                raise VaultExportError('原文图片校验失败')
            self.files[str(Path(target)/rel)] = binary

    def source_state(self, sid, version):
        current = self.source_heads.get(sid)
        overlay = self.record_heads.get(sid, {})
        metadata = self.source_metadata.get((sid,version),{})
        latest = self.source_metadata.get((sid,current['latest_version']),{}) if current else {}
        statuses = {metadata.get('status'),latest.get('status')}
        return {'stale':not current or current['latest_version'] != version,
                'expired':overlay.get('status') == 'expired' or 'expired' in statuses,
                'withdrawn':not current or bool(statuses & {'withdrawn','retracted'})}

    def related(self, sid, label, path):
        self.backlinks.setdefault(sid, []).append((label,path))

    def build(self):
        for row in sorted(self.version_rows,key=lambda r:(r['created_at'],r['version_id'])):
            sid, vid, folder = row['source_id'],row['version_id'],row['path']
            meta = json.loads(_source_file(self.root,Path(folder)/'metadata.json'))
            self.source_metadata[(sid,vid)] = meta
            text = _source_file(self.root,Path(folder)/'content.md').decode('utf-8')
            path = f'sources/{sid}/versions/{vid}/content.md'
            state = self.source_state(sid,vid)
            state['withdrawn'] |= meta.get('status') in {'withdrawn','retracted'}
            state['expired'] |= meta.get('status') == 'expired'
            revised = sid in self.sources
            self.sources.setdefault(sid,[]).append((vid,path,meta))
            self.source_pages[(sid,vid)] = path
            self.add(path,text,_metadata('source',source_id=sid,version_id=vid,title=meta['title'],
                     capture_status=meta.get('status'), original_url=meta.get('original_url'),
                     source_sha256=_hash(text.encode()), revised=revised, **state),
                     [('来源与版本目录',f'sources/{sid}/index.md'),('资料目录','资料目录.md')])
            self.assets(folder,Path(path).parent,meta)
        self._analyses()
        self._wiki()
        self._records()
        for sid, links in self.backlinks.items():
            if sid not in self.sources:
                self.add(f'sources/{sid}/index.md', '# 已撤回或缺失的来源\n\n主库当前来源记录已不可用；以下历史证据仍保留。',
                         _metadata('source_index',source_id=sid,title='已撤回来源',withdrawn=True,expired=True),
                         [('资料目录','资料目录.md'),*links])
        for sid, versions in self.sources.items():
            path = f'sources/{sid}/index.md'
            current = self.source_heads.get(sid,{})
            title = current.get('title',versions[-1][2]['title'])
            latest = current.get('latest_version')
            body = '# '+md_text(title)+'\n\n原文与AI整理、维护修订分别保存。以下链接指向本地镜像；主库仍是权威记录。\n\n## 原文版本\n\n'
            body += '\n'.join('- '+_link(('当前版本' if vid==latest else '历史版本')+' · '+str(meta.get('captured_at',vid)),path,dest) for vid,dest,meta in reversed(versions))
            self.add(path,body,_metadata('source_index',source_id=sid,version_id=latest,title=title,
                     **self.source_state(sid,latest)),[('资料目录','资料目录.md'),*self.backlinks.get(sid,[])])
        sections = [('来源资料',[(self.source_heads.get(sid,{}).get('title',versions[-1][2]['title']),f'sources/{sid}/index.md') for sid,versions in self.sources.items()]),
                    ('AI整理',self.analysis_pages),('主题Wiki',self.wiki_pages),('维护知识与修订',self.record_pages)]
        index = '# 资料目录\n\n[返回主页](主页.md)\n'
        for label, items in sections:
            index += '\n## '+label+'\n\n'+('\n'.join('- '+_link(title,'资料目录.md',path) for title,path in items) if items else '暂无记录。')+'\n'
        self.add('资料目录.md',index,_metadata('index',title='资料目录'))
        self.add('主页.md','# 知识采集库\n\n这是主知识库的关联Markdown镜像，可直接在Obsidian中打开本目录。\n\n'
                 '- [资料目录](资料目录.md)\n- [同步报告](同步报告.md)\n\n'
                 '原文、AI整理、跨来源Wiki与维护修订分别保留。AI派生内容仍需复核。\n\n'
                 '你在这里的修改会保留；后续同步遇到冲突会生成独立候选库，不会静默覆盖。'
                 '本镜像中的编辑不会自动写回主库。',_metadata('index',title='知识采集库'))
        return self.files

    def _analyses(self):
        for row in sorted(self.analysis_rows,key=lambda r:r['created_at']):
            if row['status'] not in {'complete','partial'} or not row.get('path'):
                continue
            folder = Path(row['path'])
            record = json.loads(_source_file(self.root,folder/'analysis.json'))
            sid,vid = record['source_id'],record['source_version']
            state = self.source_state(sid,vid)
            source = _source_file(self.root,folder/'source.md').decode('utf-8')
            if _hash(body_of(source).encode()) != record['input_hash']:
                raise VaultExportError('AI整理原文快照校验失败')
            meta = json.loads(_source_file(self.root,folder/'source_metadata.json'))
            target = f'analyses/{record["id"]}'
            main = target+'/analysis.md'
            for name,kind,ai in [('analysis.md','analysis',True),('evidence.md','evidence',False),('source.md','source',False)]:
                text = _source_file(self.root,folder/name).decode('utf-8')
                self.add(target+'/'+name,text,_metadata(kind,source_id=sid,version_id=vid,ai=ai,
                         title=record['title'],analysis_id=record['id'],created_at=record['created_at'],**state),
                         [('整理概览',main),('来源目录',f'sources/{sid}/index.md'),('资料目录','资料目录.md')])
            self.assets(folder,target,meta)
            self.analysis_pages.append((record['title']+' · '+record['created_at'],main))
            self.related(sid,'AI整理 · '+record['created_at'],main)

    def _wiki(self):
        for row in sorted(self.wiki_rows,key=lambda r:r['created_at']):
            if row['status'] not in {'complete','needs_review'}:
                continue
            folder=Path(row['path'])
            manifest=json.loads(_source_file(self.root,folder/'manifest.json'))
            topic,vid=manifest['topic_id'],manifest['version_id']
            target=f'wiki/{topic}/{vid}'
            page=target+'/page.md'
            state={'stale':False,'expired':False,'withdrawn':False}
            links=[]
            for sid,dep in manifest['dependencies'].items():
                check=self.source_state(sid,dep['version_id'])
                state={key:state[key] or check[key] for key in state}
                snapshot=folder/'sources'/sid
                text=_source_file(self.root,snapshot/'source.md').decode('utf-8')
                if _hash(text.encode()) != dep['hash']:
                    raise VaultExportError('Wiki原文快照校验失败')
                meta=json.loads(_source_file(self.root,snapshot/'metadata.json'))
                dest=target+'/sources/'+sid
                self.add(dest+'/source.md',text,_metadata('source',source_id=sid,version_id=dep['version_id'],title=meta['title'],**check),
                         [('主题Wiki',page),('来源目录',f'sources/{sid}/index.md')])
                self.assets(snapshot,dest,meta)
                links.append((meta['title'],f'sources/{sid}/index.md'))
                self.related(sid,'主题Wiki · '+manifest['name'],page)
            current=self.wiki_heads.get(topic,{}).get('version_id') == vid
            state['stale'] |= not current
            for name,kind,ai in [('page.md','wiki',True),('evidence.md','evidence',False)]:
                text=_source_file(self.root,folder/name).decode('utf-8')
                self.add(target+'/'+name,text,_metadata(kind,version_id=vid,title=manifest['name'],ai=ai,
                         topic_id=topic,publication_status=row['status'],is_current=current,**state),
                         [('资料目录','资料目录.md'),*links])
            self.wiki_pages.append((manifest['name']+(' · 当前' if current else ' · 历史/待复核'),page))
            if current:
                published_path=Path('wiki/topics')/(topic+'.md')
                published=_source_file(self.root,published_path)
                if _hash(published)!=self.wiki_heads[topic]['published_hash']:
                    # The library already preserves this manual edit instead of
                    # overwriting it; the mirror must not silently hide it either.
                    edited=f'wiki/{topic}/published.md'
                    body=published.decode('utf-8').replace(f'../versions/{topic}/{vid}/',vid+'/')
                    self.add(edited,body,_metadata('wiki',version_id=vid,title=manifest['name'],ai=True,
                             topic_id=topic,publication_status='modified_needs_review',tags=['人工编辑待复核'],**state),
                             [('模型原始版本',page),('资料目录','资料目录.md'),*links])
                    self.wiki_pages.append((manifest['name']+' · 人工编辑待复核',edited))

    def _records(self):
        if not self.record_rows:
            return
        from .knowledge_records import KnowledgeRecords
        records=KnowledgeRecords(self.store)
        by_record={}
        for row in sorted(self.record_versions,key=lambda r:r['created_at']):
            rid,vid=row['record_id'],row['version_id']
            doc=records.read(rid,vid)
            meta=doc['metadata']
            head=self.record_heads[rid]
            path=f'records/{rid}/versions/{vid}/content.md'
            by_record.setdefault(rid,[]).append((vid,path,meta))
            links=[('知识与修订目录',f'records/{rid}/index.md'),('资料目录','资料目录.md')]
            reference_state={'stale':False,'expired':False,'withdrawn':False}
            for ref in meta['references']:
                if 'source_id' in ref:
                    state=self.source_state(ref['source_id'],ref['version_id'])
                    reference_state={key:reference_state[key] or state[key] for key in reference_state}
                    dest=self.source_pages.get((ref['source_id'],ref['version_id']))
                    if dest:
                        links.append(('引用原文 · '+ref['version_id'],dest))
                        self.related(ref['source_id'],'维护知识 · '+meta['title'],path)
            parent=meta.get('parent_version')
            if parent:
                dest=(f'records/{rid}/versions/{parent}/content.md' if len(parent)==32 else self.source_pages.get((rid,parent)))
                if dest: links.append(('上一版本',dest))
            children=[child for child in self.record_versions if child['record_id']==rid and child['parent_version']==vid]
            links += [('后续修订',f'records/{rid}/versions/{child["version_id"]}/content.md') for child in children]
            expired=head['status']=='expired' or meta['status']=='expired' or reference_state['expired']
            body=doc['markdown']
            # An overlay may preserve original image references; keep assets beside
            # that exact source version, not a different/newest capture.
            if meta.get('source_version') and (rid,meta['source_version']) in self.source_metadata:
                original=self.source_metadata[(rid,meta['source_version'])]
                original_folder=next(row['path'] for row in self.version_rows if row['source_id']==rid and row['version_id']==meta['source_version'])
                self.assets(original_folder,Path(path).parent,original)
            external=[ref for ref in meta['references'] if 'url' in ref]
            if external:
                body+='\n\n## 未采集的外部参考\n\n'+'\n'.join('- ['+md_text(ref.get('title') or ref['url'])+']('+ref['url']+')' for ref in external)
            self.add(path,body,_metadata('record',source_id=rid if len(rid)==24 else None,version_id=vid,
                     title=meta['title'],ai=meta.get('actor')=='agent',stale=doc['stale'] or vid!=head['latest_version'] or reference_state['stale'],
                     expired=expired,withdrawn=reference_state['withdrawn'],revised=bool(parent),tags=meta['tags'],record_id=rid,parent_version=parent,
                     actor=meta.get('actor'),evidence_status=meta['evidence_status'],status=meta['status']),links)
        for rid,versions in by_record.items():
            head=self.record_heads[rid]
            path=f'records/{rid}/index.md'
            body='# '+md_text(head['title'])+'\n\n维护记录属于独立修订层，不修改采集原文。\n\n## 修订历史\n\n'
            body+='\n'.join('- '+_link(('当前' if vid==head['latest_version'] else '历史')+' · '+meta['created_at'],path,dest) for vid,dest,meta in reversed(versions))
            self.add(path,body,_metadata('record',title=head['title'],version_id=head['latest_version'],record_id=rid,
                     expired=head['status']=='expired',status=head['status']),[('资料目录','资料目录.md')])
            self.record_pages.append((head['title'],path))


def _atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as temporary:
        os.fchmod(temporary.fileno(),0o600)
        temporary.write(data)
        temporary.flush()
        os.fsync(temporary.fileno())
        staged=Path(temporary.name)
    try:
        os.replace(staged,path)
    finally:
        staged.unlink(missing_ok=True)


def _broken_links(files, destination):
    missing=[]
    for name,data in files.items():
        if not name.endswith('.md'):
            continue
        text=data.decode('utf-8')
        # Ignore fenced source examples: their Markdown is evidence, not links.
        text=re.sub(r'(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1\s*$', '',text)
        for match in re.finditer(r'!?\[[^\]\n]*\]\(([^)\n]+)\)',text):
            target=match.group(1).strip().strip('<>')
            if urlsplit(target).scheme or target.startswith('#'):
                continue
            relative=unquote(target.split('#',1)[0])
            normalized=os.path.normpath(str(Path(name).parent/relative)).replace(os.sep,'/')
            if normalized.startswith('../') or (normalized not in files and not _safe(destination,normalized).is_file()):
                missing.append({'file':name,'target':target})
    return missing


def sync_vault(store, output=None):
    """Refresh a linked reading mirror, preserving edited files and all history."""
    root=store.root.resolve()
    destination=Path(output).expanduser().absolute() if output is not None else root/'obsidian'
    for component in (destination,*destination.parents):
        if component.is_symlink():
            raise VaultExportError('镜像输出路径不允许符号链接')
    destination=destination.resolve()
    if destination==root or root.is_relative_to(destination):
        raise VaultExportError('镜像不能覆盖主知识库或其父目录')
    if destination.is_relative_to(root) and destination.relative_to(root).parts[0] in {
        'sources','analyses','wiki','records','context_alerts','interest_inferences','engine_responses','.cache'}:
        raise VaultExportError('镜像不能写入权威资料或内部处理目录')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path=_safe(destination,'.knowledge-vault.lock')
    with lock_path.open('a+b') as lock:
        os.fchmod(lock.fileno(),0o600)
        fcntl.flock(lock,fcntl.LOCK_EX)
        manifest_path=_safe(destination,MANIFEST)
        old=json.loads(manifest_path.read_text()) if manifest_path.exists() else {'managed_by':MANAGED_BY,'files':{}}
        if old.get('managed_by')!=MANAGED_BY or not isinstance(old.get('files'),dict):
            raise VaultExportError('镜像管理清单格式无效，未改动已有资料')
        planned=_Builder(store).build()
        retired=[]
        for name,entry in old['files'].items():
            if name in planned or name=='同步报告.md':
                continue
            path=_safe(destination,name)
            if name.endswith('.md') and path.is_file():
                text=path.read_text(encoding='utf-8')
                front=_front(text)
                if front.get('managed_by')!=MANAGED_BY:
                    front=_metadata('source',title='已撤回的镜像资料',withdrawn=True,expired=True)
                front.update(withdrawn=True,expired=True,stale=True,过期=True)
                front['tags']=list(dict.fromkeys([*front.get('tags',[]),'撤回']))
                planned[name]=_markdown(text,front)
                retired.append(name)
            elif path.is_file():
                # Keep prior image targets for retired historical Markdown.
                planned[name]=path.read_bytes()
        # The report is always present so homepage links are valid during checks.
        planned['同步报告.md']=_markdown('# 同步报告\n\n正在生成本次同步记录。',_metadata('index',title='同步报告'))
        broken=_broken_links(planned,destination)
        conflicts=[]
        for name,data in planned.items():
            path=_safe(destination,name)
            if path.exists() and (not path.is_file() or name not in old['files'] or _hash(path.read_bytes())!=old['files'][name]['sha256']):
                conflicts.append(name)
        candidate=None
        if conflicts:
            tree_hash=_hash(_json({name:_hash(data) for name,data in sorted(planned.items()) if name!='同步报告.md'}).encode())[:20]
            candidate=destination/'_conflicts'/tree_hash
            if candidate.exists() and any(not _safe(candidate,name).is_file() or _safe(candidate,name).read_bytes()!=data for name,data in planned.items() if name!='同步报告.md'):
                candidate=destination/'_conflicts'/(tree_hash+'-'+uuid.uuid4().hex[:8])
        report='# 同步报告\n\n本库是阅读镜像，主知识库仍是权威记录。\n\n'
        report+=f'- 本次生成：{len(planned)}个文件\n- 用户编辑冲突：{len(conflicts)}个\n- 撤回标记：{len(retired)}个\n- 未解析本地链接：{len(broken)}个\n'
        if candidate:
            report+='\n## 保留用户编辑\n\n原文件未覆盖。完整新候选库：'+_link('打开候选主页','同步报告.md',str(candidate.relative_to(destination)/'主页.md'))+'\n\n'
            report+='\n'.join('- '+_link(name,'同步报告.md',name) for name in conflicts)+'\n'
        if broken:
            report+='\n## 需检查的已有正文链接\n\n'+'\n'.join('- '+md_text(item['file']+' → '+item['target']) for item in broken)+'\n'
        planned['同步报告.md']=_markdown(report,_metadata('index',title='同步报告'))
        if candidate:
            for name,data in planned.items():
                path=_safe(candidate,name)
                if name=='同步报告.md':
                    data=_markdown('# 待复核的完整候选库\n\n[本候选主页](主页.md)\n\n用户修改的主镜像文件保持不变。本目录是同一次同步的完整生成候选，可单独在Obsidian中打开。',_metadata('index',title='候选同步报告'))
                if not path.exists(): _atomic(path,data)
        managed=dict(old['files'])
        written=unchanged=0
        for name,data in planned.items():
            path=_safe(destination,name)
            if path.exists() and (not path.is_file() or name not in old['files'] or _hash(path.read_bytes())!=old['files'][name]['sha256']):
                if name not in conflicts: conflicts.append(name)
                continue
            if path.exists() and path.read_bytes()==data:
                unchanged+=1
            else:
                _atomic(path,data); written+=1
            managed[name]={'sha256':_hash(data)}
        _atomic(manifest_path,(_json({'managed_by':MANAGED_BY,'files':managed,'last_synced_at':now()})+'\n').encode())
        return {'status':'needs_review' if conflicts or broken else 'complete','path':str(destination),
                'home':str(destination/'主页.md'),'report':str(destination/'同步报告.md'),
                'written':written,'unchanged':unchanged,'conflicts':conflicts,
                'candidate_path':str(candidate) if candidate else None,'withdrawn':retired,'broken_links':broken}


def sync_after_commit(store):
    """Best-effort mirror maintenance, strictly outside authoritative transactions.

    A separate local status file exposes failures without changing knowledge
    revision/idempotency semantics. Callers may return the compact status below.
    """
    try:
        result=sync_vault(store)
        compact={'status':result['status'],'conflict_count':len(result['conflicts']),
                 'broken_link_count':len(result['broken_links'])}
    except Exception as exc:
        compact={'status':'failed','code':getattr(exc,'code','vault_sync_failed'),
                 'error':str(exc) if isinstance(exc,VaultExportError) else 'Markdown镜像同步失败；主库已提交内容保留，可稍后重新同步。'}
        result=dict(compact)
    try:
        _atomic(_safe(store.root,'.vault-sync-status.json'),
                (_json({'updated_at':now(),**result})+'\n').encode('utf-8'))
    except Exception:
        # Even status-file failures cannot turn a committed source/revision into
        # a failed write or hide its durable idempotency result.
        pass
    return compact
