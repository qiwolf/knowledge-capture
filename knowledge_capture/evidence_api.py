"""Read exact historical evidence for authenticated workbench routes.

All returned Markdown is untrusted content; render as escaped text or with a
sanitizing renderer. Snapshot text never silently falls back to a newer source.
"""
from contextlib import closing
import json
from pathlib import Path
import re

from .processing import Processor, body_of, digest
from .wiki import Wiki
from .context_alerts import ContextAlerts


class EvidenceError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _id(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise EvidenceError('invalid_id', '证据标识格式不正确')
    return value


def _file(store, path):
    path = Path(path)
    if not path.is_absolute():
        path = store.root / path
    root = store.root.resolve()
    if not path.is_relative_to(root) or '..' in path.parts:
        raise EvidenceError('invalid_path', '证据路径不安全')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise EvidenceError('invalid_path', '证据路径不安全')
        if item == root:
            break
    if not path.is_file():
        raise EvidenceError('evidence_missing', '历史证据文件不存在；没有替换为当前来源')
    return path


def _text(store, path):
    return _file(store, path).read_text(encoding='utf-8')


def _snapshot(store, folder, sid, version, markdown_file, metadata_file=None, expected_hash=None, body_hash=False, metadata=None):
    _id(sid, r'[a-f0-9]{24}')
    _id(version, r'[a-f0-9]{24}')
    markdown = _text(store, folder / markdown_file)
    if expected_hash and digest(body_of(markdown) if body_hash else markdown) != expected_hash:
        raise EvidenceError('evidence_changed', '历史原文快照与记录哈希不一致')
    if metadata_file:
        metadata = json.loads(_text(store, folder / metadata_file))
    if metadata is not None and (metadata.get('source_id', sid) != sid or metadata.get('version_id') != version):
        raise EvidenceError('evidence_changed', '历史原文元数据版本不一致')
    return {'source_id': sid, 'version_id': version, 'markdown': markdown, 'metadata': metadata,
            'lines': [{'number': number, 'text': line} for number, line in enumerate(body_of(markdown).splitlines(), 1)]}


def read_source(store, sid, version=None):
    _id(sid, r'[a-f0-9]{24}')
    if version is not None:
        _id(version, r'[a-f0-9]{24}')
    # Store's registry resolves the requested version. Validate files before read.
    with closing(store._connect()) as db:
        row = db.execute('''SELECT v.path FROM versions v JOIN sources s ON s.id=v.source_id
            WHERE v.source_id=? AND v.version_id=COALESCE(?,s.latest_version)''', (sid, version)).fetchone()
    if row is None:
        raise EvidenceError('source_missing', '没有找到指定来源版本')
    for filename in ('content.md', 'metadata.json'):
        _file(store, Path(row['path']) / filename)
    source = store.read(sid, version)
    version = source['metadata']['version_id']
    Processor(store)
    with closing(store._connect()) as db:
        row = db.execute('''SELECT id FROM analysis_runs WHERE source_id=? AND source_version=?
            AND status IN ('complete','partial') ORDER BY created_at DESC,rowid DESC LIMIT 1''', (sid, version)).fetchone()
    analysis = read_analysis(store, row['id']) if row else None
    analysis_stale = analysis is not None and analysis['record']['input_hash'] != digest(body_of(source['markdown']))
    authority=store.source_knowledge_state(sid,version)
    return {'source': source, 'latest_analysis': analysis, 'analysis_stale': analysis_stale,
            'knowledge_authority':authority,'is_current_knowledge_evidence':authority['source_eligible']}


def read_analysis(store, run_id):
    _id(run_id, r'[a-f0-9]{32}')
    processor = Processor(store)
    with closing(store._connect()) as db:
        row = db.execute("SELECT path FROM analysis_runs WHERE id=? AND status IN ('complete','partial')", (run_id,)).fetchone()
    if row is None:
        raise EvidenceError('analysis_missing', '没有找到已完成分析')
    folder = store.root / row['path']
    for filename in ('analysis.json', 'analysis.md'):
        _file(store, folder / filename)
    result = processor.read(run_id)
    record = result['record']
    sid, version = record['source_id'], record['source_version']
    source = _snapshot(store, folder, sid, version, 'source.md', 'source_metadata.json', record['input_hash'], True)
    try:
        current = store.read(sid)
        stale = current['metadata']['version_id'] != version or digest(body_of(current['markdown'])) != record['input_hash'] or not store.source_knowledge_state(sid,version)['source_eligible']
    except (ValueError, OSError):
        stale = True
    return {**result, 'stale': stale, 'evidence_markdown': _text(store, folder / 'evidence.md'), 'sources': {sid: source}}


def read_wiki(store, topic_id, version_id=None):
    _id(topic_id, r'interest_[a-f0-9]{24}')
    if version_id is not None:
        _id(version_id, r'[a-f0-9]{32}')
    wiki = Wiki(store)
    with closing(store._connect()) as db:
        if version_id is None:
            row = db.execute('''SELECT v.* FROM wiki_versions v JOIN wiki_pages p ON v.version_id=p.version_id
                WHERE p.topic_id=?''', (topic_id,)).fetchone()
        else:
            row = db.execute('SELECT * FROM wiki_versions WHERE topic_id=? AND version_id=?', (topic_id, version_id)).fetchone()
    if row is None:
        raise EvidenceError('wiki_missing', '没有找到指定 Wiki 版本')
    folder = store.root / row['path']
    record = json.loads(_text(store, folder / 'manifest.json'))
    if record['topic_id'] != topic_id or record['version_id'] != row['version_id']:
        raise EvidenceError('evidence_changed', 'Wiki 版本记录不一致')
    if version_id is None:
        _file(store, store.root / 'wiki/topics' / f'{topic_id}.md')
        result = wiki.read(topic_id)
    else:
        result = {'topic_id': topic_id, 'version_id': row['version_id'], 'status': row['status'],
                  'markdown': _text(store, folder / 'page.md'), 'record': record,
                  'stale': not wiki._unchanged(topic_id, record['dependencies'])}
    if result['stale']:
        result['status']='needs_review'
    sources = {sid: _snapshot(store, folder / 'sources' / sid, sid, dep['version_id'], 'source.md', 'metadata.json', dep['hash'])
               for sid, dep in record['dependencies'].items()}
    return {**result, 'evidence_markdown': _text(store, folder / 'evidence.md'), 'sources': sources}


def read_alert(store, run_id):
    _id(run_id, r'[a-f0-9]{32}')
    alerts = ContextAlerts(store)
    with closing(store._connect()) as db:
        row = db.execute('SELECT path FROM context_alert_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise EvidenceError('alerts_missing', '没有找到提醒记录')
    folder = store.root / row['path']
    for filename in ('record.json', 'alerts.md'):
        _file(store, folder / filename)
    result = alerts.read(run_id)
    record = result['record']
    sources = {sid: _snapshot(store, folder, sid, dep['version_id'], f'{sid}.md', expected_hash=dep['hash'],
                             metadata=record.get('sources', {}).get(sid)) for sid, dep in record['dependencies'].items()}
    return {**result, 'evidence_markdown': _text(store, folder / 'evidence.md'), 'sources': sources}
