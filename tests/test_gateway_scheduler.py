"""Lifecycle tests use no real network providers or user library."""
from contextlib import contextmanager
import threading
import time
from unittest.mock import patch

import pytest

from knowledge_capture.gateway import create_server
from knowledge_capture.store import Store


def until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    pytest.fail('background state did not settle')


@contextmanager
def running(tmp_path):
    server = create_server(Store(tmp_path), port=0)
    server.scheduler_poll_seconds = .01
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_idle_does_not_tick(tmp_path):
    with patch('knowledge_capture.scheduler.Scheduler') as scheduler:
        scheduler.return_value.list.return_value = []
        with running(tmp_path) as server:
            until(lambda: scheduler.call_count >= 2)
            scheduler.return_value.tick.assert_not_called()
            assert server.scheduler_status()['running'] is True
            assert server.scheduler_status()['state'] == 'idle'
        assert server.scheduler_status()['running'] is False


def test_configuration_reloaded_without_restart(tmp_path):
    with patch('knowledge_capture.scheduler.Scheduler') as scheduler:
        scheduler.return_value.list.return_value = [{'enabled': 1}]
        scheduler.return_value.tick.return_value = {'status': 'processed'}
        with running(tmp_path) as server:
            with patch.object(server, 'configuration', return_value={'fresh': True}):
                until(lambda: any(call.args[1] == {'fresh': True} for call in scheduler.call_args_list))
            assert scheduler.return_value.tick.call_count > 0


def test_exception_stops_worker_and_sanitizes_error(tmp_path):
    with patch('knowledge_capture.scheduler.Scheduler') as scheduler:
        scheduler.return_value.list.side_effect = RuntimeError('secret-token-private-path')
        with running(tmp_path) as server:
            until(lambda: server.scheduler_status()['state'] == 'failed')
            until(lambda: not server.scheduler_status()['running'])
            status = server.scheduler_status()
            assert status['error']['code'] == 'scheduler_stopped'
            assert 'secret-token-private-path' not in str(status)
            assert scheduler.call_count == 1


def test_needs_review_survives_next_idle_iteration(tmp_path):
    with patch('knowledge_capture.scheduler.Scheduler') as scheduler:
        scheduler.return_value.list.return_value = [{'enabled': 1}]
        scheduler.return_value.tick.side_effect = [{'status': 'needs_review'}, {'status': 'idle'}] + [{'status': 'idle'}] * 100
        with running(tmp_path) as server:
            until(lambda: scheduler.return_value.tick.call_count >= 2)
            assert server.scheduler_status()['state'] == 'needs_review'
            assert server.scheduler_status()['error']['code'] == 'schedule_needs_review'


def test_shutdown_waits_for_tick_before_unlock(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def tick():
        entered.set()
        assert release.wait(5)
        return {'status': 'processed'}
    with patch('knowledge_capture.scheduler.Scheduler') as scheduler:
        scheduler.return_value.list.return_value = [{'enabled': 1}]
        scheduler.return_value.tick.side_effect = tick
        with running(tmp_path) as server:
            assert entered.wait(3)
            def close():
                server.shutdown()
                server.server_close()
                closed.set()
            closer = threading.Thread(target=close)
            closer.start()
            try:
                assert not closed.wait(.05)
                with pytest.raises(ValueError, match='已有采集服务'):
                    create_server(Store(tmp_path), port=0)
            finally:
                release.set()
                closer.join(3)
            assert closed.is_set()
            assert not server.scheduler_status()['running']
            replacement = create_server(Store(tmp_path), port=0)
            replacement.server_close()


def test_disabled_plan_orphan_is_reconciled_without_outbound_calls(tmp_path):
    from knowledge_capture.scheduler import Scheduler
    from contextlib import closing
    store = Store(tmp_path)
    scheduler = Scheduler(store)
    with closing(store._connect()) as db, db:
        db.execute('INSERT INTO schedules VALUES (?,3600,0,0)', ('orphan-topic',))
        db.execute("INSERT INTO schedule_runs VALUES ('orphan-run','orphan-topic','running',0,NULL,NULL,NULL)")
    with patch('knowledge_capture.discovery.Discovery.run') as outbound:
        with running(tmp_path) as server:
            until(lambda: scheduler.history()[0]['status'] == 'interrupted')
            assert scheduler.list()[0]['enabled'] == 0
            until(lambda: server.scheduler_status()['state'] == 'needs_review')
            outbound.assert_not_called()
