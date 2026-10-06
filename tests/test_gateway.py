import json
import sqlite3
import stat
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from knowledge_capture.gateway import create_server
from knowledge_capture.capture import CaptureError
from knowledge_capture.providers import ProviderError


@pytest.fixture
def gateway(tmp_path):
    store = SimpleNamespace(root=tmp_path, ingest=Mock(return_value={"status": "complete", "source_id": "source", "path": "content.md"}))
    server = create_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = "Bearer " + (tmp_path / ".api-token").read_text()
    yield SimpleNamespace(server=server, store=store, session=session, url=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    session.close()


def done(gateway, identifier):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = gateway.session.get(gateway.url + "/api/captures/" + identifier, timeout=2)
        assert response.status_code == 200
        result = response.json()
        if result["status"] in {"complete", "partial", "failed"}:
            return result
        time.sleep(0.01)
    pytest.fail("job did not finish")


def test_health_and_authentication(gateway):
    assert gateway.session.get(gateway.url + "/api/health", headers={"Authorization": None}).json() == {"status": "ok"}
    response = gateway.session.post(gateway.url + "/api/captures", headers={"Authorization": None}, json={"url": "https://example.org"})
    assert response.status_code == 401
    gateway.store.ingest.assert_not_called()
    assert stat.S_IMODE((gateway.store.root / ".api-token").stat().st_mode) == 0o600


def test_capture_terminal_and_exactly_once(gateway):
    payload = {"url": "https://example.org/article", "note": "有用资料", "idempotency_key": "retry-1"}
    response = gateway.session.post(gateway.url + "/api/captures", json=payload)
    assert response.status_code == 202
    identifier = response.json()["id"]
    terminal = done(gateway, identifier)
    assert terminal["status"] == "complete"
    assert terminal["result"]["source_id"] == "source"
    assert gateway.session.post(gateway.url + "/api/captures", json=payload).json()["id"] == identifier
    gateway.store.ingest.assert_called_once_with(url="https://example.org/article", note="有用资料", origin="user")
    response = gateway.session.post(gateway.url + "/api/captures", json={**payload, "note": "changed"})
    assert response.status_code == 409


def test_safe_error(gateway):
    gateway.store.ingest.side_effect = RuntimeError("private-token-and-server-body")
    response = gateway.session.post(gateway.url + "/api/captures", json={"url": "https://example.org"})
    terminal = done(gateway, response.json()["id"])
    assert terminal["status"] == "failed"
    assert terminal["error"]["code"] == "capture_failed"
    assert "private-token" not in json.dumps(terminal)


@pytest.mark.parametrize("payload", [{"url": "https://example.org", "origin": "discovery"}, {"url": ["https://example.org"]}, {"url": "https://a.example https://b.example"}, {"url": "file:///etc/passwd"}, {"url": "https://user:password@example.org"}, {"url": "https://example.org", "command": "execute"}])
def test_invalid_payloads(gateway, payload):
    assert gateway.session.post(gateway.url + "/api/captures", json=payload).status_code == 400
    gateway.store.ingest.assert_not_called()


def test_body_limit_and_strict_json(gateway):
    assert gateway.session.post(gateway.url + "/api/captures", data="x" * 65537, headers={"Content-Type": "application/json"}).status_code == 413
    assert gateway.session.post(gateway.url + "/api/captures", data='{"url":"https://a.example","url":"https://b.example"}', headers={"Content-Type": "application/json"}).status_code == 400


def test_cors_and_host(gateway):
    extension = "chrome-extension://" + "a" * 32
    response = gateway.session.options(gateway.url + "/api/captures", headers={"Authorization": None, "Origin": extension, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Authorization, Content-Type"})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == extension
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]
    for origin in ("https://evil.example", "null", "chrome-extension://invalid"):
        response = gateway.session.post(gateway.url + "/api/captures", headers={"Origin": origin}, json={"url": "https://example.org"})
        assert response.status_code == 403
        assert "Access-Control-Allow-Origin" not in response.headers
    for host in ("evil.example:" + str(gateway.server.server_port), "127.0.0.1:1"):
        assert gateway.session.get(gateway.url + "/api/health", headers={"Host": host}).status_code == 403


def test_chat_text_not_persisted(gateway):
    private = "这段私人聊天不可保存SECRETCHAT"
    response = gateway.session.post(gateway.url + "/api/inbox", json={"text": private + " https://example.org/article 请看看", "note": "显式备注"})
    assert response.status_code == 202
    done(gateway, response.json()["id"])
    gateway.store.ingest.assert_called_once_with(url="https://example.org/article", note="显式备注", origin="user")
    assert private.encode() not in (gateway.store.root / "inbox.sqlite3").read_bytes()
    multiple = gateway.session.post(gateway.url + "/api/inbox", json={"text": "https://a.example https://b.example"})
    assert multiple.status_code == 400


def test_bind_conflict_does_not_mark_jobs_interrupted(gateway):
    with sqlite3.connect(gateway.store.root / "inbox.sqlite3") as db:
        db.execute("INSERT INTO inbox_jobs VALUES ('existing',NULL,'hash','{}','running',NULL,NULL,'today',NULL)")
    with pytest.raises(OSError):
        create_server(gateway.store, port=gateway.server.server_port)
    with sqlite3.connect(gateway.store.root / "inbox.sqlite3") as db:
        assert db.execute("SELECT status FROM inbox_jobs WHERE id='existing'").fetchone()[0] == "running"


def test_restart_marks_unfinished_failed_and_reuses_token(tmp_path):
    store = SimpleNamespace(root=tmp_path)
    first = create_server(store, port=0)
    token = (tmp_path / ".api-token").read_text()
    with sqlite3.connect(tmp_path / "inbox.sqlite3") as db:
        db.execute("INSERT INTO inbox_jobs VALUES ('old',NULL,'hash','{}','queued',NULL,NULL,'today',NULL)")
    first.server_close()
    second = create_server(store, port=0)
    try:
        assert second.job("old")["status"] == "failed"
        assert second.job("old")["error"]["code"] == "interrupted"
        assert (tmp_path / ".api-token").read_text() == token
    finally:
        second.server_close()


def test_non_loopback_binding_rejected(tmp_path):
    with pytest.raises(ValueError):
        create_server(SimpleNamespace(root=tmp_path), host="0.0.0.0", port=0)
    assert not (tmp_path / ".api-token").exists()


def test_two_worker_limit(gateway):
    release = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    running = 0

    def ingest(**kwargs):
        nonlocal running
        with lock:
            running += 1
            if running == 2:
                started.set()
        release.wait(3)
        with lock:
            running -= 1
        return {"status": "complete"}

    gateway.store.ingest.side_effect = ingest
    identifiers = []
    try:
        for index in range(4):
            response = gateway.session.post(gateway.url + "/api/captures", json={"url": f"https://example.org/{index}"})
            identifiers.append(response.json()["id"])
        assert started.wait(1)
        assert gateway.store.ingest.call_count == 2
        assert gateway.server.job(identifiers[-1])["status"] == "queued"
    finally:
        release.set()
    assert all(done(gateway, identifier)["status"] == "complete" for identifier in identifiers)


def test_configuration_router_forwarded(tmp_path):
    from knowledge_capture.providers import Configuration
    store = SimpleNamespace(root=tmp_path, ingest=Mock(return_value={"status": "partial"}))
    router = Mock()
    configuration = Configuration({"services": {}, "capabilities": {}})
    with patch("knowledge_capture.providers.CaptureRouter", return_value=router) as factory:
        server = create_server(store, port=0, configuration=configuration)
        try:
            submitted = server.enqueue({"url": "https://example.org/", "note": "", "origin": "user"}, None)
            server.executor.shutdown(wait=True)
            assert server.job(submitted["id"])["status"] == "partial"
            factory.assert_called_once_with(configuration, store=store)
            assert store.ingest.call_args.kwargs["capture_fn"] is router.capture
        finally:
            server.server_close()


def test_second_port_same_root_does_not_touch_live_jobs(gateway):
    with sqlite3.connect(gateway.store.root / "inbox.sqlite3") as db:
        db.execute("INSERT INTO inbox_jobs VALUES ('live',NULL,'hash','{}','running',NULL,NULL,'today',NULL)")
    token_before = (gateway.store.root / ".api-token").read_bytes()
    with pytest.raises(ValueError, match="已有采集服务"):
        create_server(gateway.store, port=0)
    assert gateway.server.job("live")["status"] == "running"
    assert (gateway.store.root / ".api-token").read_bytes() == token_before


@pytest.mark.parametrize("failure", [CaptureError("no_content", "未取得有效正文，请完成来源验证。"), ProviderError("configuration_missing", "采集连接器尚未配置。")])
def test_safe_capture_error_details_preserved(gateway, failure):
    gateway.store.ingest.side_effect = failure
    response = gateway.session.post(gateway.url + "/api/captures", json={"url": "https://example.org/article"})
    terminal = done(gateway, response.json()["id"])
    assert terminal["status"] == "failed"
    assert terminal["error"] == {"code": failure.code, "message": str(failure)}


class PipelineModel:
    identity = {"provider": "test-fixture", "model": "not-real-ai"}

    def __init__(self, topics=1, fail=None):
        self.topics, self.fail, self.calls = topics, fail, []

    def complete_json(self, system, payload):
        from knowledge_capture.llm import LLMError
        phase = "analysis" if "lines" in payload else "alerts" if "facts" in payload else "wiki"
        self.calls.append((phase, payload))
        if phase == self.fail:
            raise LLMError("model_failed", "测试模型处理失败。")
        if phase == "analysis":
            line = payload["lines"][0]
            citation = {"start_line": line["number"], "end_line": line["number"], "quote": line["text"]}
            return {"summary": [{"text": "设备更新须核实版本", "evidence": [citation]}], "key_points": [],
                    "topics": [{"name": f"设备版本主题{number}", "reason": "资料涉及设备版本", "evidence": [citation]} for number in range(self.topics)], "questions": []}
        if phase == "alerts":
            return {"alerts": []}
        source = payload["sources"][0]
        line = source["lines"][0]
        citation = {"source_id": source["source_id"], "version_id": source["version_id"],
                    "start_line": line["number"], "end_line": line["number"], "quote": line["text"]}
        citation.pop("quote")
        return {"summary": [{"text": "设备更新需要核实版本", "evidence": [citation]}], "agreements": [], "differences": [], "questions": []}


def run_pipeline(tmp_path, model=None, confirmed=True, auto=True, missing=False):
    from knowledge_capture.store import Store
    from knowledge_capture.context_alerts import ContextAlerts
    from knowledge_capture.llm import LLMError
    store = Store(tmp_path)
    if confirmed:
        ContextAlerts(store).set_fact("设备", "当前版本", "1.0")
    body = "设备升级前需要核实当前版本，保存配置备份并检查兼容性。"
    capture = {"title": "设备升级资料", "markdown": body, "original_url": "https://example.org/article",
               "final_url": "https://example.org/article", "author": None, "published_at": None,
               "status": "complete", "assets": [], "warnings": []}
    server = create_server(store, port=0, auto_process=auto)
    try:
        kwargs = {"side_effect": LLMError("configuration_missing", "请配置模型服务。")} if missing else {"return_value": model}
        with patch("knowledge_capture.capture.capture_url", return_value=capture), patch("knowledge_capture.llm.CloudClient.from_env", **kwargs) as factory:
            job = server.enqueue({"url": "https://example.org/article", "note": "私密收藏备注", "origin": "user"}, None)
            server.executor.shutdown(wait=True)
            if not auto:
                factory.assert_not_called()
        return store, server.job(job["id"]), body
    finally:
        server.server_close()


def test_auto_process_real_pipeline_with_mock_model(tmp_path):
    from pathlib import Path
    model = PipelineModel()
    store, job, body = run_pipeline(tmp_path, model)
    result = job["result"]
    assert job["status"] == "complete"
    assert result["capture"]["status"] == "complete"
    assert result["analysis"]["status"] == "complete"
    assert result["wiki"]["status"] == "complete"
    assert len(result["wiki"]["pages"]) == 1
    assert result["alerts"]["status"] == "complete"
    assert Path(result["analysis"]["result"]["path"]).exists()
    assert Path(result["wiki"]["pages"][0]["path"]).exists()
    assert Path(result["alerts"]["result"]["path"]).exists()
    assert body in store.read(result["source_id"])["markdown"]
    assert [phase for phase, _ in model.calls] == ["analysis", "wiki", "alerts"]
    assert len(model.calls[-1][1]["sources"]) == 1
    assert "私密收藏备注" not in json.dumps(model.calls, ensure_ascii=False)


@pytest.mark.parametrize("phase", ["analysis", "wiki", "alerts"])
def test_auto_phase_failure_preserves_capture(tmp_path, phase):
    model = PipelineModel(fail=phase)
    store, job, body = run_pipeline(tmp_path, model)
    result = job["result"]
    assert job["status"] == "partial"
    assert result["capture"]["status"] == "complete"
    assert result[phase]["status"] == "failed"
    assert result[phase]["error"]["code"] == "model_failed"
    assert any(phase in warning for warning in result["warnings"])
    assert body in store.read(result["source_id"])["markdown"]
    assert sum(name == phase for name, _ in model.calls) == 1


def test_auto_missing_credentials_is_partial_not_capture_failure(tmp_path):
    store, job, body = run_pipeline(tmp_path, missing=True)
    result = job["result"]
    assert job["status"] == "partial"
    assert result["analysis"]["error"]["code"] == "configuration_missing"
    assert result["wiki"]["status"] == "skipped"
    assert result["alerts"]["status"] == "skipped"
    assert body in store.read(result["source_id"])["markdown"]


def test_auto_max_three_wikis_and_no_context_skip(tmp_path):
    model = PipelineModel(topics=4)
    _, job, _ = run_pipeline(tmp_path, model, confirmed=False)
    result = job["result"]
    assert job["status"] == "partial"
    assert result["wiki"]["status"] == "partial"
    assert len(result["wiki"]["pages"]) == 3
    assert len(result["wiki"]["skipped_topic_ids"]) == 1
    assert result["alerts"]["status"] == "skipped"
    assert [phase for phase, _ in model.calls].count("wiki") == 3
    assert any("3 个主题" in warning for warning in result["warnings"])


def test_auto_off_never_creates_model_client(tmp_path):
    _, job, _ = run_pipeline(tmp_path, auto=False)
    assert job["status"] == "complete"
    assert "analysis" not in job["result"]
