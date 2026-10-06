import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from knowledge_capture.workbench_exports import prepare_export, ExportError
from test_portable import library


def assert_local_images(z):
    for name in z.namelist():
        if name.endswith(('metadata.json', 'source_metadata.json')):
            metadata = json.loads(z.read(name))
            for asset in metadata['assets']:
                if asset['status'] == 'complete':
                    image_name = str(Path(name).parent / asset['relative_path'])
                    assert hashlib.sha256(z.read(image_name)).hexdigest() == asset['sha256']


@pytest.mark.parametrize('kind', ['source', 'analysis', 'wiki'])
def test_download_zip_has_markdown_and_local_images(tmp_path, kind):
    store, source, analysis, topic, wiki = library(tmp_path / 'library')
    identifier = {'source': source['source_id'], 'analysis': analysis['id'], 'wiki': topic}[kind]
    Path(wiki['path']).write_text(Path(wiki['path']).read_text() + '\n人工补充保留。')
    (store.root / '.settings.json').write_text('SECRET')
    with prepare_export(store, kind, identifier) as download:
        archive_path = download.path
        assert download.content_type == 'application/zip'
        assert download.size == archive_path.stat().st_size
        assert download.filename.endswith('.zip')
        with zipfile.ZipFile(archive_path) as z:
            assert all(not n.startswith('/') and '..' not in Path(n).parts for n in z.namelist())
            assert any(n.endswith('.md') for n in z.namelist())
            assert any(n.endswith('assets/image.png') for n in z.namelist())
            assert not any('.settings' in n for n in z.namelist())
            assert_local_images(z)
            if kind == 'wiki':
                page = f'wiki/topics/{topic}.md'
                assert '人工补充保留' in z.read(page).decode()
                # Published relative source/evidence paths resolve inside the bundle.
                import re
                for link in re.findall(r'\]\(([^)]+)\)', z.read(page).decode()):
                    resolved = (tmp_path / Path(page).parent / link.split('#')[0]).resolve().relative_to(tmp_path)
                    assert resolved.as_posix() in z.namelist()
    assert not archive_path.exists()


def test_cleanup_even_when_http_stream_fails(tmp_path):
    store, source, *_ = library(tmp_path / 'library')
    with pytest.raises(ConnectionError):
        with prepare_export(store, 'source', source['source_id']) as download:
            path = download.path
            raise ConnectionError('client disconnected')
    assert not path.exists()


def test_library_uses_portable_backup(tmp_path):
    store, *_ = library(tmp_path / 'library')
    with prepare_export(store, 'library') as download:
        with zipfile.ZipFile(download.path) as z:
            assert 'library-manifest.json' in z.namelist()
            assert 'index.sqlite3' in z.namelist()


@pytest.mark.parametrize('identifier', ['../.settings.json', '/etc/passwd', 'abc.zip\r\nX:bad', 'sources/foo'])
def test_invalid_id_rejected(tmp_path, identifier):
    store, *_ = library(tmp_path / 'library')
    with pytest.raises(ExportError):
        with prepare_export(store, 'source', identifier):
            pytest.fail('unsafe identifier accepted')


def test_symlink_assets_rejected_and_unknown_files_excluded(tmp_path):
    store, source, *_ = library(tmp_path / 'library')
    folder = Path(source['path']).parent
    (folder / 'private.json').write_text('secret not associated with source')
    with prepare_export(store, 'source', source['source_id']) as download:
        with zipfile.ZipFile(download.path) as z:
            assert not any('private.json' in n for n in z.namelist())
    image = folder / 'assets/image.png'
    original = tmp_path / 'image.png'
    original.write_bytes(image.read_bytes())
    image.unlink()
    image.symlink_to(original)
    with pytest.raises(ExportError, match='符号链接'):
        with prepare_export(store, 'source', source['source_id']):
            pytest.fail('symlink accepted')


def test_database_path_escape_rejected(tmp_path):
    store, source, *_ = library(tmp_path / 'library')
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE versions SET path='../../secret'")
    with pytest.raises(ExportError, match='路径'):
        with prepare_export(store, 'source', source['source_id']):
            pytest.fail('database path accepted')
