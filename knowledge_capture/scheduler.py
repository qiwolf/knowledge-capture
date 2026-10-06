"""Persisted, opt-in schedules. Unknown interrupted work requires manual resume."""
from contextlib import closing
import fcntl
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .discovery import Discovery, DiscoveryError
from .processing import Processor


class Scheduler:
    def __init__(self, store, configuration=None, clock=time.time, configuration_loader=None):
        self.store, self.configuration, self.clock = store, configuration, clock
        self.configuration_loader = configuration_loader
        self.processor = Processor(store)
        with closing(store._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schedules (
                    topic_id TEXT PRIMARY KEY, interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, next_due REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id TEXT PRIMARY KEY, topic_id TEXT NOT NULL, status TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, discovery_id TEXT,
                    result_json TEXT);
            ''')

    def set(self, topic_id, interval_hours=24, enabled=True):
        if type(interval_hours) is not int or not 1 <= interval_hours <= 720:
            raise ValueError('检索间隔应为1至720个整小时')
        if type(enabled) is not bool:
            raise ValueError('启用状态必须为布尔值')
        topics = {t['id']: t for t in self.processor.interests()}
        if topic_id not in topics or (enabled and not topics[topic_id]['eligible_for_discovery']):
            raise ValueError('启用定时检索前请先关注该主题')
        with closing(self.store._connect()) as db, db:
            if enabled and db.execute("SELECT 1 FROM schedule_runs WHERE topic_id=? AND status='running'", (topic_id,)).fetchone():
                raise ValueError('该主题仍有运行中的任务；先检查工作进程和运行记录')
            db.execute('INSERT INTO schedules VALUES (?, ?, ?, ?) ON CONFLICT(topic_id) DO UPDATE SET interval_seconds=excluded.interval_seconds,enabled=excluded.enabled,next_due=excluded.next_due',
                       (topic_id, interval_hours * 3600, int(enabled), self.clock()))
        return next(s for s in self.list() if s['topic_id'] == topic_id)

    def list(self):
        with closing(self.store._connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM schedules ORDER BY next_due,topic_id')]

    def history(self):
        with closing(self.store._connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM schedule_runs ORDER BY started_at DESC,rowid DESC')]

    def _check_alerts(self, result):
        from .context_alerts import ContextAlerts
        from .llm import CloudClient
        sources = sorted({c['source_id'] for c in result.get('candidates', [])
                          if c.get('status') == 'added' and c.get('source_id')})
        if not sources:
            return {'status': 'skipped', 'reason': 'no_new_sources'}
        context = ContextAlerts(self.store)
        if not any(f['status'] == 'confirmed' for f in context.list_facts()):
            return {'status': 'skipped', 'reason': 'no_confirmed_context'}
        owner = Discovery(self.store, self.configuration)
        client = CloudClient.for_store(self.store)
        class BudgetClient:
            identity = client.identity
            def complete_json(self, system, payload):
                day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
                owner._reserve(day, 'model_calls')
                return client.complete_json(system, payload)
        return context.analyze(sources, BudgetClient())

    def tick(self, runner=None):
        if self.configuration_loader is not None:
            self.configuration = self.configuration_loader()
        # Kernel lock proves prior worker is gone before marking interrupted work.
        with (self.store.root / '.scheduler.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'busy', 'runs': []}
            with closing(self.store._connect()) as db, db:
                interrupted = [r['topic_id'] for r in db.execute("SELECT topic_id FROM schedule_runs WHERE status='running'")]
                for topic_id in interrupted:
                    db.execute('UPDATE schedules SET enabled=0 WHERE topic_id=?', (topic_id,))
                db.execute("UPDATE schedule_runs SET status='interrupted',finished_at=?,result_json=? WHERE status='running'",
                           (self.clock(), json.dumps({'error': '进程中断，结果可能已部分写入；检查检索记录后手动重新启用'}, ensure_ascii=False)))
                due = [dict(r) for r in db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_due<=? ORDER BY next_due,topic_id LIMIT 5', (self.clock(),))]
            results = []
            for schedule in due:
                topic_id = schedule['topic_id']
                with closing(self.store._connect()) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT * FROM schedules WHERE topic_id=?', (topic_id,)).fetchone()
                    if not current['enabled'] or current['next_due'] > self.clock():
                        continue
                    eligible = any(t['id'] == topic_id and t['eligible_for_discovery'] for t in self.processor.interests())
                    if not eligible:
                        db.execute('UPDATE schedules SET enabled=0 WHERE topic_id=?', (topic_id,))
                        results.append({'topic_id': topic_id, 'status': 'paused'})
                        continue
                    run_id = uuid.uuid4().hex
                    db.execute("INSERT INTO schedule_runs VALUES (?, ?, 'running', ?, NULL, NULL, NULL)", (run_id, topic_id, self.clock()))
                    db.execute('UPDATE schedules SET next_due=? WHERE topic_id=?', (self.clock() + current['interval_seconds'], topic_id))
                try:
                    result = (runner or Discovery(self.store, self.configuration).run)(topic_id)
                    if not isinstance(result, dict) or result.get('status') not in {'complete', 'partial', 'no_new', 'no_results'}:
                        raise ValueError('检索返回状态不正确')
                    status = result['status']
                    if runner is None:
                        from .context_alerts import ContextError
                        from .llm import LLMError
                        try:
                            result['alerts'] = self._check_alerts(result)
                        except Exception as exc:
                            known = isinstance(exc, (ContextError, LLMError))
                            result['alerts'] = {'status': 'failed', 'error_code': exc.code if known else 'alerts_failed',
                                                'error': str(exc) if known else '关联提醒未完成，请检查运行记录'}
                            status = 'partial'
                except Exception as exc:
                    result = {'error_code': getattr(exc, 'code', 'schedule_failed'),
                              'error': str(exc) if isinstance(exc, DiscoveryError) else '定时检索失败，请检查检索记录'}
                    status = 'failed'
                with closing(self.store._connect()) as db, db:
                    db.execute('UPDATE schedule_runs SET status=?,finished_at=?,discovery_id=?,result_json=? WHERE id=?',
                               (status, self.clock(), result.get('id'), json.dumps(result, ensure_ascii=False), run_id))
                    # Failure and partial results need review, not unattended retries.
                    if status in {'failed', 'partial'}:
                        db.execute('UPDATE schedules SET enabled=0 WHERE topic_id=?', (topic_id,))
                results.append({'id': run_id, 'topic_id': topic_id, 'status': status, 'result': result})
            status = ('needs_review' if interrupted or any(r['status'] in {'failed', 'partial'} for r in results)
                      else 'processed' if results else 'idle')
            return {'status': status, 'interrupted_topics': interrupted, 'runs': results}

    def work(self, emit=print, poll_seconds=30):
        if not 1 <= poll_seconds <= 60:
            raise ValueError('轮询间隔应在1至60秒之间')
        while True:
            result = self.tick()
            if result['runs'] or result.get('interrupted_topics'):
                emit(json.dumps(result, ensure_ascii=False))
            time.sleep(poll_seconds)
