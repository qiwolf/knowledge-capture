"""Bounded download preparation with automatic temporary-file cleanup.

Call ``with prepare_export(store, kind, identifier) as download:`` and stream
``download.path`` inside the block. Authentication belongs to the HTTP handler.
No archive or filename supplied by a caller is used as an output filesystem path.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from .portable import export_library, PortableError, _validate_records

MAX_DOCUMENT_BYTES = 512 * 1024**2


class ExportError(ValueError):
    code = 'export_failed'


@dataclass(frozen=True)
class Download:
    filename: str
    path: Path
    size: int
    content_type: str = 'application/zip'


def _identifier(value, topic=False):
    pattern = r'interest_[a-f0-9]{24}' if topic else r'[a-f0-9]{24,32}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ExportError('导出标识不正确')
    return value


def _path(root, relative):
    if not isinstance(relative, str) or '\\' in relative or ':' in relative:
        raise ExportError('导出文件路径不合法')
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in {'.', '..'} or p.startswith('.') for p in parts):
        raise ExportError('导出文件路径不合法')
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ExportError('导出文件不能包含符号链接')
    if not path.resolve().is_relative_to(root):
        raise ExportError('导出文件超出知识库')
    return path


def _read(root, relative):
    path = _path(root, relative)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES:
            raise ExportError('导出文件类型或大小不支持')
        body = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ExportError('导出文件超过大小限制')
    return body


def _bundle(store, kind, identifier, version, output):
    root = store.root.resolve()
    payload = {}
    total = 0
    originals = {}
    def add(relative, archive_name=None):
        nonlocal total
        body = _read(root, relative)
        total += len(body)
        if total > MAX_DOCUMENT_BYTES:
            raise ExportError('文档导出超过512 MiB限制，请使用全库备份')
        originals[relative] = hashlib.sha256(body).hexdigest()
        payload[archive_name or relative] = body
        return body
    def document(relative, metadata_name, required):
        for filename in required:
            add(f'{relative}/{filename}')
        metadata = json.loads(add(f'{relative}/{metadata_name}'))
        from .store import raw_evidence_files
        for name, digest in raw_evidence_files(metadata, root / relative):
            body = add(f'{relative}/{name}')
            if hashlib.sha256(body).hexdigest() != digest:
                raise ExportError('原始响应校验失败，未生成导出文件')
        for asset in metadata.get('assets', []):
            if asset.get('status') != 'complete':
                continue
            name = asset['relative_path']
            parts = PurePosixPath(name).parts
            if len(parts) != 2 or parts[0] != 'assets':
                raise ExportError('图片引用路径不正确')
            body = add(f'{relative}/{name}')
            if hashlib.sha256(body).hexdigest() != asset['sha256']:
                raise ExportError('图片校验失败，未生成导出文件')
    with closing(store._connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        if kind == 'source':
            row = db.execute('SELECT v.path,v.version_id FROM versions v JOIN sources s ON s.id=v.source_id WHERE s.id=? AND v.version_id=COALESCE(?,s.latest_version)', (identifier, version)).fetchone()
            if row is None:
                raise ExportError('找不到要导出的来源版本')
            _identifier(row['version_id'])
            expected = f'sources/{identifier}/versions/{row["version_id"]}'
            if row['path'] != expected:
                raise ExportError('来源路径与版本不匹配')
            document(expected, 'metadata.json', ['content.md'])
        elif kind == 'analysis':
            row = db.execute("SELECT * FROM analysis_runs WHERE id=? AND status IN ('complete','partial')", (identifier,)).fetchone()
            if row is None:
                raise ExportError('找不到已完成的整理结果')
            _identifier(row['source_id'])
            _identifier(row['source_version'])
            expected = f'analyses/{row["source_id"]}/{row["source_version"]}/{identifier}'
            if row['path'] != expected:
                raise ExportError('整理路径与版本不匹配')
            document(expected, 'source_metadata.json', ['analysis.md', 'analysis.json', 'source.md', 'evidence.md'])
        elif kind == 'record':
            from .processing import md_text
            tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            loaded = _validate_records(db, root)
            head = db.execute('SELECT * FROM knowledge_records WHERE id=?', (identifier,)).fetchone() if 'knowledge_records' in tables else None
            selected = version or (head['latest_version'] if head else None)
            if head is None or (selected is not None and len(selected) == 24):
                row = db.execute('SELECT v.path,v.version_id FROM versions v JOIN sources s ON s.id=v.source_id WHERE s.id=? AND v.version_id=COALESCE(?,s.latest_version)', (identifier, selected)).fetchone()
                if row is None:
                    raise ExportError('找不到要导出的知识或来源版本')
                expected = f'sources/{identifier}/versions/{row["version_id"]}'
                if row['path'] != expected:
                    raise ExportError('来源路径与版本不匹配')
                document(expected, 'metadata.json', ['content.md'])
                title = json.loads(payload[expected+'/metadata.json'])['title']
                index = '# '+md_text(title)+'\n\n[采集原文与图片]('+expected+'/content.md)\n'
                exported = {'record_id':identifier,'version_id':row['version_id'],'kind':'captured_source'}
            else:
                if (identifier,selected) not in loaded:
                    raise ExportError('找不到指定知识维护版本')
                chain, cursor = [], selected
                while (identifier,cursor) in loaded:
                    entry = loaded[(identifier,cursor)]
                    chain.append(entry)
                    cursor = entry['row']['parent_version']
                copied_sources = set()
                reference_links = []
                for entry in chain:
                    row, metadata = entry['row'], entry['metadata']
                    document(row['path'], 'metadata.json', ['content.md'])
                    for ref in metadata['references']:
                        if 'source_id' not in ref:
                            continue
                        sid, vid = ref['source_id'],ref['version_id']
                        expected = f'sources/{sid}/versions/{vid}'
                        if (sid,vid) not in copied_sources:
                            source = db.execute('SELECT path FROM versions WHERE source_id=? AND version_id=?',(sid,vid)).fetchone()
                            if source is None or source['path']!=expected:
                                raise ExportError('知识维护原文引用缺失')
                            document(expected,'metadata.json',['content.md'])
                            copied_sources.add((sid,vid))
                            source_meta=json.loads(payload[expected+'/metadata.json'])
                            reference_links.append((source_meta['title'],expected+'/content.md'))
                        # Source overlays retain relative image references in their
                        # unchanged Markdown; copy verified original assets beside it.
                        if sid==identifier and vid==metadata.get('source_version'):
                            source_meta=json.loads(payload[expected+'/metadata.json'])
                            for asset in source_meta.get('assets',[]):
                                if asset.get('status')=='complete':
                                    add(expected+'/'+asset['relative_path'],row['path']+'/'+asset['relative_path'])
                latest = chain[0]
                index = '# '+md_text(latest['metadata']['title'])+'\n\n维护内容属于修订层，不能替代采集原文。\n\n'
                index += '[所选版本]('+latest['row']['path']+'/content.md) · 状态：'+latest['metadata']['status']+'\n\n## 修订历史\n\n'
                index += '\n'.join('- ['+entry['row']['version_id']+']('+entry['row']['path']+'/content.md)' for entry in chain)
                index += '\n\n## 引用原文及原始响应\n\n'+'\n'.join('- ['+md_text(title)+']('+path+')' for title,path in reference_links)+'\n'
                exported = {'record_id':identifier,'version_id':selected,'kind':'maintained_knowledge',
                            'versions':[entry['row']['version_id'] for entry in chain],
                            'events':[dict(row) for row in db.execute('SELECT * FROM knowledge_record_events WHERE record_id=? ORDER BY created_at,rowid',(identifier,)) if row['version_id'] in {entry['row']['version_id'] for entry in chain}]}
            payload['知识记录.md']=index.encode('utf-8')
            exported['files']={name:hashlib.sha256(body).hexdigest() for name,body in payload.items()}
            payload['record-export.json']=json.dumps(exported,ensure_ascii=False,indent=2).encode('utf-8')
        else:
            row = db.execute('SELECT v.path,v.version_id FROM wiki_pages p JOIN wiki_versions v ON p.version_id=v.version_id WHERE p.topic_id=?', (identifier,)).fetchone()
            if row is None:
                raise ExportError('找不到已发布的 Wiki')
            _identifier(row['version_id'])
            expected = f'wiki/versions/{identifier}/{row["version_id"]}'
            if row['path'] != expected:
                raise ExportError('Wiki 路径与版本不匹配')
            add(f'wiki/topics/{identifier}.md')
            for name in ('page.md', 'evidence.md'):
                add(f'{expected}/{name}')
            manifest = json.loads(add(f'{expected}/manifest.json'))
            for sid in manifest['dependencies']:
                _identifier(sid)
                document(f'{expected}/sources/{sid}', 'metadata.json', ['source.md'])
        # Manual source/page editing does not necessarily take a DB lock.
        for relative, digest in originals.items():
            if hashlib.sha256(_read(root, relative)).hexdigest() != digest:
                raise ExportError('导出期间文档变化，请停止编辑后重试')
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for relative, body in sorted(payload.items()):
                archive.writestr(relative, body)


@contextmanager
def prepare_export(store, kind, identifier=None, *, version=None):
    """Yield a download valid only within this context, including on disconnect.

    ``version`` applies to source and maintained-record exports. Wiki exports preserve the current
    published page (including manual edits) plus its source/image snapshots.
    User-added external links stay links; unrelated local files are not included.
    """
    if kind not in {'source', 'analysis', 'wiki', 'record', 'library'}:
        raise ExportError('不支持的导出类型')
    if kind == 'library':
        if identifier is not None or version is not None:
            raise ExportError('全库导出不接受文档标识')
    else:
        _identifier(identifier, topic=kind == 'wiki')
        if version is not None:
            if kind not in {'source','record'}:
                raise ExportError('只有来源或知识维护导出支持指定版本')
            _identifier(version)
    filename = f'knowledge-{kind}' + (f'-{identifier}' if identifier else '') + '.zip'
    with tempfile.TemporaryDirectory(prefix='kc-download-') as temporary:
        output = Path(temporary) / filename
        try:
            if kind == 'library':
                export_library(store, output)
            else:
                _bundle(store, kind, identifier, version, output)
        except (ExportError, PortableError):
            raise
        except Exception:
            raise ExportError('导出失败：请检查文档、图片及数据库完整性') from None
        yield Download(filename=filename, path=output, size=output.stat().st_size)
