"""Cross-document hypotheses use original analyses and survive relocation."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile

import pytest

from knowledge_capture.discovery import Discovery
from knowledge_capture.interest_inference import InterestInference
from knowledge_capture.portable import export_library, restore_library, PortableError
from knowledge_capture.processing import Processor
from knowledge_capture.store import Store
from knowledge_capture.wiki import Wiki
from test_interest_inference import add, Infer
from test_wiki import Synthesizer


def seed(root):
    store = Store(root)
    docs = [add(store, number, text) for number, text in enumerate([
        'Python数字与字符串表达式入门。', 'Python条件和循环流程控制。', 'Python列表和字典数据结构。'])]
    record = InterestInference(store).infer(Infer())
    return store, docs, record


def test_cross_document_topic_uses_original_sources_for_wiki_and_discovery(tmp_path):
    store, docs, inferred = seed(tmp_path)
    processor = Processor(store)
    before = [(entry['id'], Path(store.root / entry['path'] / 'analysis.json').read_bytes()) for entry in processor.history()]
    topic = next(t for t in processor.interests() if t['name'] == 'Python基础编程学习')
    assert len(topic['evidence']) == 3
    assert {r['source_id'] for r in Discovery(store)._known(topic['name'], topic['id'])} == {d['source_id'] for d in docs}
    result = Wiki(store).build(topic['id'], Synthesizer(), protocol="quote-v1")
    page = Wiki(store).read(topic['id'])
    assert not page['stale']
    assert set(page['record']['dependencies']) == {d['source_id'] for d in docs}
    assert len(processor.history()) == len(before)
    assert before == [(entry['id'], Path(store.root / entry['path'] / 'analysis.json').read_bytes()) for entry in processor.history()]
    # Invalidating an inference must not keep its old source membership alive.
    add(store, 0, 'Python数字资料已经换成完全不同的新版本内容。')
    topic_after = next((t for t in processor.interests() if t['id'] == topic['id']), None)
    assert topic_after is None or not topic_after['evidence']
    assert Wiki(store).read(topic['id'])['stale']
    assert Discovery(store)._known(topic['name'], topic['id']) == []


def test_inference_backup_restore_after_original_deleted(tmp_path):
    store, docs, inferred = seed(tmp_path / 'original')
    topic = next(t for t in Processor(store).interests() if t['name'] == 'Python基础编程学习')
    Processor(store).feedback(topic['id'], 'followed')
    Wiki(store).build(topic['id'], Synthesizer(), protocol="quote-v1")
    file = store.root / 'interest_inferences' / inferred['id'] / 'inference.json'
    saved = file.read_bytes()
    archive = export_library(store, tmp_path / 'library.zip')
    with zipfile.ZipFile(archive) as z:
        assert z.read(f'interest_inferences/{inferred["id"]}/inference.json') == saved
    shutil.rmtree(store.root)
    restored = Store(restore_library(archive, tmp_path / 'restored'))
    assert (restored.root / 'interest_inferences' / inferred['id'] / 'inference.json').read_bytes() == saved
    assert InterestInference(restored).valid_records()[0]['id'] == inferred['id']
    restored_topic = next(t for t in Processor(restored).interests() if t['id'] == topic['id'])
    assert restored_topic['state'] == 'followed'
    assert restored_topic['user_source_count'] == 3
    assert not Wiki(restored).read(topic['id'])['stale']
    assert len(Discovery(restored)._known(topic['name'], topic['id'])) == 3


def test_busy_or_missing_inference_rejected_by_backup(tmp_path):
    store, _, inferred = seed(tmp_path / 'original')
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE interest_inference_runs SET status='running'")
    with pytest.raises(PortableError, match='运行中'):
        export_library(store, tmp_path / 'busy.zip')
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE interest_inference_runs SET status='complete'")
    (store.root / 'interest_inferences' / inferred['id'] / 'inference.json').unlink()
    with pytest.raises(PortableError, match='关联文档'):
        export_library(store, tmp_path / 'missing.zip')


def test_retry_inference_history_survives_portable_restore(tmp_path):
    from knowledge_capture.llm import LLMError
    store = Store(tmp_path / 'original')
    for number, text in enumerate(['Python数字与字符串表达式入门。', 'Python条件和循环流程控制。', 'Python列表和字典数据结构。']):
        add(store, number, text)
    inference = InterestInference(store)
    class Failure:
        identity = Infer.identity
        def complete_json(self, system, payload):
            raise LLMError('http_error', '测试失败')
    with pytest.raises(LLMError):
        inference.infer(Failure())
    with closing(store._connect()) as db:
        failed = db.execute("SELECT id FROM interest_inference_runs WHERE status='failed'").fetchone()[0]
    result = inference.retry_failed(failed, Infer())
    assert result['retry_of'] == failed
    archive = export_library(store, tmp_path / 'retry.zip')
    shutil.rmtree(store.root)
    restored = Store(restore_library(archive, tmp_path / 'restored'))
    current = InterestInference(restored).valid_records()[0]
    assert current['id'] == result['id'] and current['retry_of'] == failed
    with closing(restored._connect()) as db:
        assert db.execute('SELECT status FROM interest_inference_runs WHERE id=?', (failed,)).fetchone()[0] == 'failed'


def test_backup_rejects_inference_missing_original_version(tmp_path):
    store, _, inferred = seed(tmp_path / 'original')
    file = store.root / 'interest_inferences' / inferred['id'] / 'inference.json'
    record = json.loads(file.read_text())
    record['sources'][0]['version_id'] = 'f' * 24
    file.write_text(json.dumps(record))
    with pytest.raises(PortableError, match='原来源版本'):
        export_library(store, tmp_path / 'incomplete.zip')
