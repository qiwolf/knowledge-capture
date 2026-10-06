"""Portable, credential-free library snapshots. Restore never merges into a library.

Stop writers/editors before export. SQLite write locks protect committed rows while
files are copied; file hashes also detect changes during the snapshot. Credentials
must be configured again and schedules explicitly re-enabled after restoration.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import zipfile

from .store import now

DIRECTORIES = {'sources', 'analyses', 'wiki', 'context_alerts', 'interest_inferences', 'records'}
DATABASES = {'index.sqlite3', 'inbox.sqlite3'}
MAX_FILES = 100000
MAX_TOTAL = 4 * 1024**3
MAX_FILE = 512 * 1024**2
MANIFEST = 'library-manifest.json'


class PortableError(ValueError):
    pass


def _relative(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise PortableError('归档路径不合法')
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in name.split('/')):
        raise PortableError('归档路径不合法')
    if any(p.startswith('.') for p in path.parts):
        raise PortableError('归档不能包含隐藏或临时文件')
    if name not in DATABASES and path.parts[0] not in DIRECTORIES:
        raise PortableError('归档包含不支持的文件；配置与凭据不得迁移')
    return path


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise PortableError('知识库包含符号链接或特殊文件，无法安全备份')


def _hash(path):
    _regular(path)
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _validate_records(db, root):
    """Validate authoritative maintenance history independently of ZIP hashes."""
    names = {'knowledge_records', 'knowledge_record_versions', 'knowledge_record_events', 'knowledge_record_idempotency'}
    present = names & _tables(db)
    if not present:
        return {}
    if present != names:
        raise PortableError('知识维护数据库缺少必要记录表')
    def rows(table):
        cursor = db.execute('SELECT * FROM ' + table)
        fields = [column[0] for column in cursor.description]
        return [dict(zip(fields, row)) for row in cursor]
    heads = {row['id']: row for row in rows('knowledge_records')}
    versions = {(row['record_id'], row['version_id']): row for row in rows('knowledge_record_versions')}
    loaded = {}
    try:
        for (rid, vid), row in versions.items():
            if (not re.fullmatch(r'(?:[a-f0-9]{24}|[a-f0-9]{32})', rid)
                    or not re.fullmatch(r'[a-f0-9]{32}', vid) or rid not in heads
                    or row['path'] != f'records/{rid}/versions/{vid}'):
                raise ValueError('identity')
            relative = _relative(row['path'])
            folder = root / relative
            content, metadata_file = folder / 'content.md', folder / 'metadata.json'
            if not content.is_file() or not metadata_file.is_file():
                raise ValueError('payload')
            if _hash(content) != row['markdown_sha256'] or _hash(metadata_file) != row['metadata_sha256']:
                raise ValueError('hash')
            metadata = json.loads(metadata_file.read_text(encoding='utf-8'))
            if (metadata['record_id'] != rid or metadata['version_id'] != vid
                    or metadata['parent_version'] != row['parent_version']
                    or metadata['status'] not in {'active', 'expired'}
                    or metadata['origin'] != 'maintained_knowledge'
                    or metadata['evidence_status'] != 'authored_record_not_original_source'
                    or not isinstance(metadata['title'], str) or not metadata['title'].strip()
                    or not isinstance(metadata['references'], list)
                    or not isinstance(metadata['tags'], list)):
                raise ValueError('metadata')
            source_version = metadata.get('source_version')
            found_original = source_version is None
            for ref in metadata['references']:
                if 'source_id' not in ref:
                    from .store import canonical_url
                    if ref.get('evidence_status') != 'external_reference_not_captured' or canonical_url(ref['url']) != ref['url']:
                        raise ValueError('external reference')
                    continue
                source = db.execute('SELECT path FROM versions WHERE source_id=? AND version_id=?',
                                    (ref['source_id'], ref['version_id'])).fetchone()
                if source is None:
                    raise ValueError('source reference')
                source_path = _relative(source[0])
                if str(source_path) != f'sources/{ref["source_id"]}/versions/{ref["version_id"]}':
                    raise ValueError('source identity')
                if _hash(root / source_path / 'content.md') != ref['markdown_sha256']:
                    raise ValueError('source hash')
                if ref['source_id'] == rid and ref['version_id'] == source_version:
                    found_original = True
            if not found_original:
                raise ValueError('overlay original')
            parent = row['parent_version']
            if parent is not None and (rid, parent) not in versions:
                if source_version is None or parent != source_version or db.execute(
                        'SELECT 1 FROM versions WHERE source_id=? AND version_id=?', (rid, parent)).fetchone() is None:
                    raise ValueError('parent')
            loaded[(rid, vid)] = {'row': row, 'metadata': metadata}
        for rid, head in heads.items():
            entry = loaded.get((rid, head['latest_version']))
            if entry is None or head['status'] != entry['metadata']['status'] or head['title'] != entry['metadata']['title']:
                raise ValueError('head')
        for key in versions:
            visited, cursor = set(), key
            while cursor in versions:
                if cursor in visited:
                    raise ValueError('parent cycle')
                visited.add(cursor)
                cursor = (cursor[0], versions[cursor]['parent_version'])
        audited = set()
        for event in rows('knowledge_record_events'):
            key = (event['record_id'], event['version_id'])
            if key not in loaded or event['event'] not in {'created','revised','tagged','expired','reactivated'}:
                raise ValueError('event')
            metadata = loaded[key]['metadata']
            if any(event[field] != metadata[field] for field in ('actor','note','created_at')):
                raise ValueError('event metadata')
            audited.add(key)
        if audited != set(loaded):
            raise ValueError('missing event')
        for entry in rows('knowledge_record_idempotency'):
            result = json.loads(entry['response_json'])
            key = (result['record_id'], result['version_id'])
            if key in loaded:
                actual = loaded[key]
                if result['metadata'] != actual['metadata'] or result['markdown'] != (root / actual['row']['path'] / 'content.md').read_text(encoding='utf-8'):
                    raise ValueError('idempotency payload')
            else:
                source = db.execute('SELECT path FROM versions WHERE source_id=? AND version_id=?', key).fetchone()
                if source is None:
                    raise ValueError('idempotency version')
                original = (root / _relative(source[0]) / 'content.md').read_text(encoding='utf-8')
                if result['markdown'] != original.split('---\n\n', 1)[-1]:
                    raise ValueError('idempotency source payload')
    except (OSError, ValueError, TypeError, KeyError):
        raise PortableError('知识维护版本、元数据、哈希或来源修订链校验失败') from None
    return loaded


def _validate(root):
    """Check portable DB pointers and required payloads before publishing."""
    with closing(sqlite3.connect(f'{(root / "index.sqlite3").as_uri()}?mode=ro', uri=True)) as db:
        tables = _tables(db)
        if not {'sources', 'versions', 'captures'} <= tables:
            raise PortableError('不是受支持的知识库数据库')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise PortableError('数据库完整性检查失败')
        specs = [('versions', 'sources', ['content.md', 'metadata.json']),
                 ('analysis_runs', 'analyses', ['analysis.md', 'analysis.json', 'source.md', 'source_metadata.json', 'evidence.md']),
                 ('wiki_versions', 'wiki', ['page.md', 'manifest.json', 'evidence.md']),
                 ('context_alert_runs', 'context_alerts', ['record.json', 'alerts.md', 'evidence.md']),
                 ('interest_inference_runs', 'interest_inferences', ['inference.json']),
                 ('knowledge_record_versions', 'records', ['content.md', 'metadata.json'])]
        for table, prefix, required in specs:
            if table not in tables:
                continue
            for (name,) in db.execute(f'SELECT path FROM {table} WHERE path IS NOT NULL'):
                relative = _relative(name)
                if relative.parts[0] != prefix:
                    raise PortableError('数据库文件引用超出对应文档目录')
                for filename in required:
                    if not (root / relative / filename).is_file():
                        raise PortableError('归档缺失数据库关联文档')
        if db.execute('SELECT 1 FROM sources s LEFT JOIN versions v ON s.id=v.source_id AND s.latest_version=v.version_id WHERE v.version_id IS NULL LIMIT 1').fetchone():
            raise PortableError('归档缺失来源最新版本')
        if 'interest_inference_runs' in tables:
            for run_id, input_hash, name in db.execute("SELECT id,input_hash,path FROM interest_inference_runs WHERE status='complete'"):
                if not name:
                    raise PortableError('归档缺失共同关注归纳结果路径')
                relative = _relative(name)
                if relative.parts[0] != 'interest_inferences':
                    raise PortableError('共同关注归纳路径不合法')
                try:
                    record = json.loads((root / relative / 'inference.json').read_text(encoding='utf-8'))
                    if record['id'] != run_id or record['input_hash'] != input_hash.split(':attempt:', 1)[0] or record['status'] != 'complete':
                        raise ValueError()
                    for source in record['sources']:
                        if db.execute('SELECT 1 FROM versions WHERE source_id=? AND version_id=?', (source['source_id'], source['version_id'])).fetchone() is None:
                            raise ValueError()
                except (OSError, ValueError, TypeError, KeyError):
                    raise PortableError('共同关注归纳记录或原来源版本不完整') from None
        _validate_records(db, root)
        if 'wiki_pages' in tables:
            for (topic,) in db.execute('SELECT topic_id FROM wiki_pages'):
                path = _relative(f'wiki/topics/{topic}.md')
                if not (root / path).is_file():
                    raise PortableError('归档缺失已发布 Wiki')
    for file in root.rglob('*.json'):
        if file.name not in {'metadata.json', 'source_metadata.json'}:
            continue
        metadata = json.loads(file.read_text(encoding='utf-8'))
        from .store import raw_evidence_files
        try:
            raw_evidence_files(metadata, file.parent)
        except ValueError as exc:
            raise PortableError(str(exc)) from None
        for asset in metadata.get('assets', []):
            if asset.get('status') != 'complete':
                continue
            name = asset['relative_path']
            parts = PurePosixPath(name).parts
            if len(parts) != 2 or parts[0] != 'assets' or '\\' in name or parts[1] in {'.', '..'}:
                raise PortableError('图片引用不合法')
            image = file.parent / name
            if not image.is_file() or _hash(image) != asset['sha256']:
                raise PortableError('图片缺失或内容校验失败')


def _relocate_history(database, root, restoring=False):
    """Relocate only known JSON path fields, without changing notes/source prose."""
    marker = 'kc-library:/'
    def convert(value):
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == 'path' and isinstance(item, str):
                if restoring and item.startswith(marker):
                    item = str(root / _relative(item[len(marker):]))
                elif not restoring:
                    path = Path(item)
                    if path.is_absolute() and path.is_relative_to(root):
                        item = marker + path.relative_to(root).as_posix()
            result[key] = convert(item)
        return result
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('PRAGMA journal_mode=DELETE')
        tables = _tables(db)
        for table, column in [('inbox_jobs', 'result'), ('action_jobs', 'result'), ('schedule_runs', 'result_json'), ('discovery_wiki', 'result_json')]:
            if table not in tables:
                continue
            for rowid, packed in db.execute(f'SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL').fetchall():
                value = convert(json.loads(packed))
                db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (json.dumps(value, ensure_ascii=False), rowid))


def _discard_derived_indexes(database):
    """Portable authority excludes embedding vectors and their cache generations."""
    with closing(sqlite3.connect(database)) as db:
        tables = _tables(db)
        discard = [name for name in ('retrieval_vectors','retrieval_runs') if name in tables]
        if not discard:
            return
        db.execute('PRAGMA secure_delete=ON')
        with db:
            for name in discard:
                db.execute('DELETE FROM '+name)
        # Removed cache payloads should not survive in free pages of the copied DB.
        db.execute('VACUUM')


def export_library(store_or_root, output):
    root = Path(store_or_root if isinstance(store_or_root, (str, os.PathLike)) else store_or_root.root).expanduser().resolve()
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise PortableError('导出文件已存在，不覆盖')
    if root == output.resolve() or root in output.resolve().parents:
        raise PortableError('备份文件必须位于知识库之外')
    if not (root / 'index.sqlite3').is_file():
        raise PortableError('知识库不存在')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kc-backup-') as temporary, ExitStack() as stack:
        staging = Path(temporary)
        for name in sorted(DATABASES):
            path = root / name
            if not path.exists():
                continue
            _regular(path)
            lock = stack.enter_context(closing(sqlite3.connect(path, timeout=1)))
            try:
                lock.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError:
                raise PortableError('知识库正忙，请停止写入后再备份') from None
            for table in sorted(_tables(lock) & {'captures', 'analysis_runs', 'discovery_runs', 'schedule_runs', 'inbox_jobs', 'action_jobs', 'interest_inference_runs'}):
                if lock.execute(f"SELECT 1 FROM {table} WHERE status IN ('running','queued') LIMIT 1").fetchone():
                    raise PortableError('存在运行中或排队任务，请完成或处理后再备份')
            with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as source, closing(sqlite3.connect(staging / name)) as dest:
                source.backup(dest)
            _relocate_history(staging / name, root)
            _discard_derived_indexes(staging / name)
        copied = {}
        for directory in sorted(DIRECTORIES):
            base = root / directory
            if base.is_symlink():
                raise PortableError('知识库目录不能是符号链接')
            if not base.exists():
                continue
            for folder, dirs, files in os.walk(base, followlinks=False):
                for name in dirs:
                    if (Path(folder) / name).is_symlink():
                        raise PortableError('知识库目录不能是符号链接')
                dirs[:] = [n for n in dirs if not n.startswith('.')]
                for name in sorted(files):
                    if name.startswith('.') or name.startswith('tmp'):
                        continue
                    source = Path(folder) / name
                    relative = source.relative_to(root).as_posix()
                    _relative(relative)
                    digest = _hash(source)
                    target = staging / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    if _hash(target) != digest:
                        raise PortableError('备份期间文件改变，请停止编辑后重试')
                    copied[source] = digest
        for source, digest in copied.items():
            if _hash(source) != digest:
                raise PortableError('备份期间文件改变，请停止编辑后重试')
        _validate(staging)
        files = {f.relative_to(staging).as_posix(): {'size': f.stat().st_size, 'sha256': _hash(f)} for f in staging.rglob('*') if f.is_file()}
        if len(files) > MAX_FILES or sum(x['size'] for x in files.values()) > MAX_TOTAL or any(x['size'] > MAX_FILE for x in files.values()):
            raise PortableError('知识库超过当前便携备份大小限制')
        manifest = {'format': 'knowledge-capture-library', 'version': 1, 'created_at': now(), 'files': files,
                    'excluded': ['credentials', 'provider configuration', 'temporary files', 'obsidian reading mirror', 'vector and model caches'], 'restore_schedules': 'disabled'}
        # Stage the finished archive and publish exclusively, never leaving a half backup.
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.kc-export-', delete=False) as handle:
            archive_path = Path(handle.name)
        try:
            with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False))
                for name in sorted(files):
                    archive.write(staging / name, name)
            os.link(archive_path, output)
        finally:
            archive_path.unlink(missing_ok=True)
    return output


def restore_library(archive_path, destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise PortableError('恢复目录必须不存在，不合并或覆盖已有目录')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the final name exclusively, so publication cannot overwrite a racing creator.
    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [i.filename for i in entries]
            if len(entries) > MAX_FILES + 1 or len(names) != len(set(names)) or MANIFEST not in names:
                raise PortableError('归档条目数量或清单不合法')
            if len({n.casefold() for n in names}) != len(names):
                raise PortableError('归档路径存在大小写冲突')
            for info in entries:
                if info.filename != MANIFEST:
                    _relative(info.filename)
                mode = info.external_attr >> 16
                if info.is_dir() or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}) or info.flag_bits & 1:
                    raise PortableError('归档包含链接、目录、特殊文件或加密条目')
                if info.file_size > MAX_FILE or info.file_size > max(1, info.compress_size) * 1000:
                    raise PortableError('归档文件超过安全解压限制')
            if sum(i.file_size for i in entries) > MAX_TOTAL or archive.getinfo(MANIFEST).file_size > 32 * 1024**2:
                raise PortableError('归档超过安全解压限制')
            manifest = json.loads(archive.read(MANIFEST))
            if manifest.get('format') != 'knowledge-capture-library' or manifest.get('version') != 1:
                raise PortableError('不支持的知识库归档版本')
            files = manifest.get('files')
            if not isinstance(files, dict) or set(files) != set(names) - {MANIFEST} or 'index.sqlite3' not in files:
                raise PortableError('归档文件与清单不一致')
            for name, details in files.items():
                info = archive.getinfo(name)
                if not isinstance(details, dict) or details.get('size') != info.file_size:
                    raise PortableError('归档文件大小与清单不一致')
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(info) as src, target.open('xb') as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b''):
                        size += len(chunk)
                        if size > info.file_size:
                            raise PortableError('归档文件超过声明大小')
                        digest.update(chunk)
                        dst.write(chunk)
                if digest.hexdigest() != details.get('sha256'):
                    raise PortableError('归档文件内容校验失败')
        _validate(destination)
        for name in DATABASES:
            if not (destination / name).exists():
                continue
            _relocate_history(destination / name, destination, restoring=True)
            _discard_derived_indexes(destination / name)
            with closing(sqlite3.connect(destination / name)) as db, db:
                if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise PortableError('数据库完整性检查失败')
                if 'schedules' in _tables(db):
                    db.execute('UPDATE schedules SET enabled=0')
    except Exception:
        shutil.rmtree(destination)
        raise
    return destination
