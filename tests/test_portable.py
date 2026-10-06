import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import zipfile

import pytest

from knowledge_capture.portable import export_library, restore_library, PortableError, MANIFEST
from knowledge_capture.store import Store
from knowledge_capture.processing import Processor
from knowledge_capture.wiki import Wiki
from test_wiki import Analyzer, Synthesizer


def library(root):
    store = Store(root)
    def capture(url, assets):
        image = b'example-image-fixture'
        (assets / 'image.png').write_bytes(image)
        return {'title': '系统更新', 'markdown': '版本升级前需要备份配置。\n\n![图片](assets/image.png)', 'final_url': url,
                'author': None, 'published_at': None, 'status': 'complete', 'warnings': [],
                'assets': [{'status': 'complete', 'relative_path': 'assets/image.png', 'sha256': hashlib.sha256(image).hexdigest()}]}
    source = store.ingest('https://example.org/portable', note='用户备注', capture_fn=capture)
    processor = Processor(store)
    analysis = processor.analyze(source['source_id'], Analyzer())
    topic = processor.interests()[0]['id']
    # Single source summary rather than a cross-source difference.
    synth = Synthesizer(lambda result: result.update(differences=[]))
    wiki = Wiki(store).build(topic, synth, protocol="quote-v1")
    return store, source, analysis, topic, wiki


def test_roundtrip_without_original_library(tmp_path):
    store, source, analysis, topic, wiki = library(tmp_path / 'original')
    page = Path(wiki['path'])
    page.write_text(page.read_text() + '\n人工整理内容。')
    (store.root / '.api-token').write_text('DO-NOT-EXPORT')
    (store.root / 'providers.json').write_text('SECRET')
    (store.root / '.capture-temp').mkdir()
    (store.root / '.capture-temp/private').write_text('TEMP')
    archive = export_library(store, tmp_path / 'backup.zip')
    with zipfile.ZipFile(archive) as z:
        assert not any('token' in n or 'providers' in n or '.capture' in n for n in z.namelist())
    shutil.rmtree(store.root)
    target = restore_library(archive, tmp_path / 'restored')
    restored = Store(target)
    doc = restored.read(source['source_id'])
    assert (Path(doc['path']) / 'assets/image.png').read_bytes() == b'example-image-fixture'
    assert restored.search('备份')[0]['source_id'] == source['source_id']
    assert restored.captures()[0]['note'] == '用户备注'
    assert Processor(restored).read(analysis['id'])['record']['ai_processed']
    page = Wiki(restored).read(topic)
    assert not page['stale']
    assert page['modified'] and '人工整理' in page['markdown']
    assert not (target / '.api-token').exists()


def test_existing_targets_untouched(tmp_path):
    store = Store(tmp_path / 'original')
    archive = export_library(store, tmp_path / 'backup.zip')
    before = archive.read_bytes()
    with pytest.raises(PortableError):
        export_library(store, archive)
    assert archive.read_bytes() == before
    with pytest.raises(PortableError):
        restore_library(archive, store.root)
    assert Store(store.root).list_sources() == []


def test_active_work_and_symlinks_refused(tmp_path):
    store = Store(tmp_path / 'original')
    job = store.begin_capture('https://example.org')
    with pytest.raises(PortableError, match='运行中'):
        export_library(store, tmp_path / 'backup.zip')
    store.fail_capture(job, 'cancelled', '已取消')
    (store.root / 'sources').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PortableError, match='符号链接'):
        export_library(store, tmp_path / 'backup.zip')
    assert not (tmp_path / 'backup.zip').exists()


@pytest.mark.parametrize('name', ['../outside', '/outside', 'sources/../../outside', 'sources\\outside', '.api-token', 'providers.json', 'sources/a/../b', 'sources//b'])
def test_malicious_paths_rejected(tmp_path, name):
    path = tmp_path / 'malicious.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(MANIFEST, '{}')
        z.writestr(name, 'attack')
    with pytest.raises(PortableError):
        restore_library(path, tmp_path / 'target')
    assert not (tmp_path / 'target').exists()
    assert not (tmp_path / 'outside').exists()


def test_symlink_archive_rejected(tmp_path):
    path = tmp_path / 'malicious.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(MANIFEST, '{}')
        link = zipfile.ZipInfo('sources/link')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, '/etc/passwd')
    with pytest.raises(PortableError):
        restore_library(path, tmp_path / 'target')


def test_tampered_payload_rejected(tmp_path):
    store = Store(tmp_path / 'original')
    archive = export_library(store, tmp_path / 'backup.zip')
    with zipfile.ZipFile(archive) as z:
        content = {n: z.read(n) for n in z.namelist()}
    content['index.sqlite3'] = b'x' * len(content['index.sqlite3'])
    with zipfile.ZipFile(tmp_path / 'tampered.zip', 'w') as z:
        for name, value in content.items():
            z.writestr(name, value)
    with pytest.raises(PortableError, match='校验'):
        restore_library(tmp_path / 'tampered.zip', tmp_path / 'target')
    assert not (tmp_path / 'target').exists()


def test_schedules_and_context_preserved_but_disabled(tmp_path):
    store = Store(tmp_path / 'original')
    from knowledge_capture.scheduler import Scheduler
    from knowledge_capture.context_alerts import ContextAlerts
    Scheduler(store)
    ContextAlerts(store).set_fact('路由器', '系统版本', '6.49')
    with sqlite3.connect(store.db_path) as db:
        db.execute("INSERT INTO schedules VALUES ('topic', 3600, 1, 0)")
    archive = export_library(store, tmp_path / 'backup.zip')
    restored = Store(restore_library(archive, tmp_path / 'restored'))
    assert Scheduler(restored).list()[0]['enabled'] == 0
    with sqlite3.connect(restored.db_path) as db:
        assert db.execute('SELECT value FROM context_facts').fetchone()[0] == '6.49'
    assert Scheduler(store).list()[0]['enabled'] == 1


def test_compression_bomb_rejected(tmp_path):
    path = tmp_path / 'bomb.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, '{}')
        z.writestr('sources/bomb.md', b'0' * 2000000)
    with pytest.raises(PortableError, match='限制'):
        restore_library(path, tmp_path / 'target')


def test_wal_snapshot_and_history_paths_relocate(tmp_path):
    store, source, *_ = library(tmp_path / 'original')
    # Keep WAL connection live: copying the main file alone would omit this row.
    db = sqlite3.connect(store.db_path)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE captures SET note='WAL中的新备注'")
        db.commit()
        with sqlite3.connect(store.root / 'inbox.sqlite3') as inbox:
            inbox.execute('CREATE TABLE inbox_jobs (id TEXT, status TEXT, result TEXT)')
            inbox.execute('INSERT INTO inbox_jobs VALUES (?, ?, ?)', ('job', 'complete', json.dumps({'path': source['path']})))
        archive = export_library(store, tmp_path / 'backup.zip')
    finally:
        db.close()
    shutil.rmtree(store.root)
    target = restore_library(archive, tmp_path / 'restored')
    restored = Store(target)
    assert restored.captures()[0]['note'] == 'WAL中的新备注'
    with sqlite3.connect(target / 'inbox.sqlite3') as inbox:
        result = json.loads(inbox.execute('SELECT result FROM inbox_jobs').fetchone()[0])
    assert Path(result['path']).is_file()
    assert Path(result['path']).is_relative_to(target)


def test_missing_associated_payload_prevents_backup(tmp_path):
    store, source, *_ = library(tmp_path / 'original')
    Path(source['path']).unlink()
    with pytest.raises(PortableError, match='关联文档'):
        export_library(store, tmp_path / 'backup.zip')
    assert not (tmp_path / 'backup.zip').exists()


def test_workbench_actions_block_export_and_paths_relocate(tmp_path):
    store, source, *_ = library(tmp_path / 'original')
    with sqlite3.connect(store.root / 'inbox.sqlite3') as db:
        db.execute('CREATE TABLE action_jobs (id TEXT, status TEXT, result TEXT)')
        db.execute('INSERT INTO action_jobs VALUES (?, ?, ?)', ('action', 'running', None))
    with pytest.raises(PortableError, match='运行中'):
        export_library(store.root, tmp_path / 'backup.zip')
    with sqlite3.connect(store.root / 'inbox.sqlite3') as db:
        db.execute('UPDATE action_jobs SET status=?, result=?', ('complete', json.dumps({'path': source['path']})))
    archive = export_library(store.root, tmp_path / 'backup.zip')
    destination = restore_library(archive, tmp_path / 'restored')
    with sqlite3.connect(destination / 'inbox.sqlite3') as db:
        restored = json.loads(db.execute('SELECT result FROM action_jobs').fetchone()[0])
    assert Path(restored['path']).is_relative_to(destination)
    assert Path(restored['path']).is_file()
