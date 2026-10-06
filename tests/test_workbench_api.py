import hashlib
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from knowledge_capture.gateway import create_server
from knowledge_capture.store import Store


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path)
    server = create_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = "Bearer " + (tmp_path / ".api-token").read_text()
    yield SimpleNamespace(store=store, server=server, session=session, url=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    server.server_close()
    thread.join(2)
    session.close()


def terminal(app, identifier):
    for _ in range(100):
        result = app.session.get(app.url + "/api/actions/" + identifier).json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.01)
    pytest.fail("action not finished")


def ingest(store, image=False):
    def capture(url, assets):
        data = b"\x89PNG\r\n\x1a\nfixture"
        if image:
            (assets / "frame.png").write_bytes(data)
        return {"title": "测试来源", "markdown": "设备版本信息需要保持更新。", "original_url": url, "final_url": url,
                "author": None, "published_at": None, "status": "complete", "warnings": [],
                "assets": [{"relative_path": "assets/frame.png", "original_url": "https://example.org/image", "status": "complete",
                            "sha256": hashlib.sha256(data).hexdigest()}] if image else []}
    return store.ingest("https://example.org/a", capture_fn=capture)


def test_same_origin_allowed_others_forbidden(app):
    response = app.session.get(app.url + "/api/overview", headers={"Origin": app.url})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == app.url
    for origin in ("http://evil.example", "https://127.0.0.1:" + str(app.server.server_port), "http://127.0.0.1:1"):
        assert app.session.get(app.url + "/api/overview", headers={"Origin": origin}).status_code == 403
    assert app.session.get(app.url + "/api/overview", headers={"Authorization": None}).status_code == 401


def test_overview_and_source_read(app):
    item = ingest(app.store)
    overview = app.session.get(app.url + "/api/overview").json()
    assert overview["sources"][0]["id"] == item["source_id"]
    assert overview["capabilities"]["scheduler_running"] is True
    source = app.session.get(app.url + "/api/sources/" + item["source_id"]).json()
    assert source["source"]["metadata"]["version_id"] == item["version_id"]
    assert source["latest_analysis"] is None
    historical = app.session.get(app.url + f"/api/sources/{item['source_id']}/versions/{item['version_id']}").json()
    assert historical['source']['markdown'] == source['source']['markdown']
    assert historical['analysis_stale'] is False


def test_historical_evidence_routes(app):
    routes = [('/api/analyses/' + 'a' * 32, 'read_analysis', ('a' * 32,)),
              ('/api/wiki/interest_' + 'b' * 24 + '/versions/' + 'c' * 32, 'read_wiki', ('interest_' + 'b' * 24, 'c' * 32)),
              ('/api/alerts/' + 'd' * 32, 'read_alert', ('d' * 32,))]
    for route, method, args in routes:
        with patch('knowledge_capture.evidence_api.' + method, return_value={'evidence_markdown': '精确历史摘录'}) as read:
            response = app.session.get(app.url + route)
            assert response.status_code == 200
            assert response.json()['evidence_markdown'] == '精确历史摘录'
            read.assert_called_once_with(app.store, *args)


def test_static_allowlist_and_security_headers(app):
    with patch("knowledge_capture.gateway.Path") as path:
        file = path.return_value.parent.__truediv__.return_value.__truediv__.return_value
        file.is_file.return_value = True
        file.is_symlink.return_value = False
        file.read_bytes.return_value = b"static fixture"
        response = app.session.get(app.url + "/", headers={"Authorization": None})
    assert response.status_code == 200
    assert response.content == b"static fixture"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert app.session.get(app.url + "/.api-token").status_code == 404


def test_persistent_action_failure_preserves_source(app):
    from knowledge_capture.llm import LLMError
    item = ingest(app.store)
    with patch("knowledge_capture.processing.Processor.analyze", side_effect=LLMError("configuration_missing", "请先配置模型。")):
        response = app.session.post(app.url + "/api/actions", json={"action": "analyze", "identifier": item["source_id"]})
        assert response.status_code == 202
        job = terminal(app, response.json()["id"])
    assert job["status"] == "failed" and job["error"]["code"] == "configuration_missing"
    assert app.store.read(item["source_id"])["metadata"]["status"] == "complete"
    with sqlite3.connect(app.store.root / "inbox.sqlite3") as db:
        assert db.execute("SELECT status FROM action_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "failed"


def test_async_success_and_strict_action_fields(app):
    with patch("knowledge_capture.processing.Processor.analyze", return_value={"status": "complete", "id": "run"}):
        response = app.session.post(app.url + "/api/actions", json={"action": "analyze", "identifier": "a" * 24})
        assert terminal(app, response.json()["id"])["result"]["id"] == "run"
    for value in ({"action": "execute", "command": "anything"}, {"action": "analyze", "identifier": "a" * 24, "path": "/secret"}, {"action": "alerts-check", "source_ids": ["../../"]}):
        assert app.session.post(app.url + "/api/actions", json=value).status_code == 400


def test_context_setting_sync(app):
    response = app.session.post(app.url + "/api/actions", json={"action": "context-set", "subject": "设备", "field": "版本", "value": "1.0"})
    assert response.status_code == 200
    assert response.json()["result"]["value"] == "1.0"
    assert len(app.session.get(app.url + "/api/overview").json()["facts"]) == 1


def test_asset_registered_hash_and_symlink_enforcement(app):
    item = ingest(app.store, image=True)
    prefix = app.url + f"/api/assets/{item['source_id']}/{item['version_id']}/"
    assert app.session.get(prefix + "frame.png").content.startswith(b"\x89PNG")
    assert app.session.get(prefix + "frame.png", headers={"Authorization": None}).status_code == 401
    assert app.session.get(prefix + "unregistered.png").status_code == 404
    path = app.store.root / "sources" / item["source_id"] / "versions" / item["version_id"] / "assets" / "frame.png"
    path.write_bytes(b"modified")
    assert app.session.get(prefix + "frame.png").status_code == 400
    path.unlink()
    path.symlink_to(app.store.root / ".api-token")
    assert app.session.get(prefix + "frame.png").status_code == 400


def test_authenticated_zip_download_and_invalid_route(app):
    import io
    import zipfile
    item = ingest(app.store, image=True)
    route = app.url + '/api/export/source/' + item['source_id']
    assert app.session.get(route, headers={'Authorization': None}).status_code == 401
    response = app.session.get(route)
    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/zip'
    assert response.headers['Content-Disposition'].startswith('attachment;')
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert any(name.endswith('/content.md') for name in archive.namelist())
        assert any(name.endswith('/assets/frame.png') for name in archive.namelist())
        assert not any('.api-token' in name for name in archive.namelist())
    assert app.session.get(app.url + '/api/export/library/invalid').status_code == 400
    assert app.session.get(app.url + '/api/export/source/no-file-path').status_code == 400


def test_model_settings_secret_never_returns_or_enters_jobs(app):
    with patch.dict("os.environ", {}, clear=True):
        response = app.session.post(app.url + "/api/actions", json={"action": "settings-model", "base_url": "https://model.example/v1", "model": "test", "api_key": "SECRET-NOT-FOR-OUTPUT"})
        assert response.status_code == 200
        assert "SECRET-NOT-FOR-OUTPUT" not in response.text
        settings = app.session.get(app.url + "/api/settings").json()
        assert settings["model"]["configured"] is True
        assert "SECRET-NOT-FOR-OUTPUT" not in json.dumps(settings)
    assert b"SECRET-NOT-FOR-OUTPUT" not in (app.store.root / "inbox.sqlite3").read_bytes()


def test_auto_processing_preference_changes_live_capture_behavior(app):
    assert not app.server.effective_auto_process()
    response = app.session.post(app.url + '/api/actions', json={'action':'settings-preferences', 'auto_process':True})
    assert response.status_code == 200
    assert app.server.effective_auto_process()
    with patch.object(app.store, 'ingest', return_value={'status':'complete'}) as capture, patch.object(app.server, 'process_capture', return_value={'status':'complete','analysis':{'status':'complete'}}) as process:
        response = app.session.post(app.url + '/api/captures', json={'url':'https://example.org/runtime','note':'运行时偏好验证'})
        identifier = response.json()['id']
        for _ in range(100):
            job = app.session.get(app.url + '/api/captures/' + identifier).json()
            if job['status'] not in {'running','queued'}:
                break
            time.sleep(.01)
        assert job['status'] == 'complete' and job['result']['analysis']['status'] == 'complete'
        assert job['url'] == 'https://example.org/runtime' and job['note'] == '运行时偏好验证'
        process.assert_called_once()
    app.session.post(app.url + '/api/actions', json={'action':'settings-preferences','auto_process':False}).raise_for_status()
    assert not app.server.effective_auto_process()
    app.server.auto_process = True
    assert app.server.effective_auto_process()
    assert app.server.workbench.overview()['capabilities']['auto_process_override']


def test_search_job_missing_config_is_not_empty_results(app):
    response = app.session.post(app.url + "/api/actions", json={"action": "search-web", "query": "公开搜索词", "limit": 2})
    assert response.status_code == 202
    job = terminal(app, response.json()["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "configuration_missing"


def test_library_full_text_search_and_strict_query(app):
    item = ingest(app.store)
    response = app.session.get(app.url + "/api/search", params={"q": "版本信息", "limit": 5})
    assert response.status_code == 200
    assert response.json()["results"][0]["source_id"] == item["source_id"]
    for suffix in ("?q=a&q=b", "?q=", "?q=a&limit=0", "?q=a&path=secret", "?limit=1"):
        assert app.session.get(app.url + "/api/search" + suffix).status_code == 400
    overview = app.session.get(app.url + "/api/overview").json()
    assert overview["totals"]["sources"] == 1
    assert overview["has_more"]["sources"] is False


def test_configured_search_uses_latest_configuration(app):
    from knowledge_capture.providers import Configuration
    observed = []
    class FixtureConnector:
        def __init__(self, service):
            pass
        def call(self, payload):
            observed.append(payload)
            return {"results": [{"title": "真实合约测试", "url": "https://example.org/result", "description": "测试固定响应"}]}
    app.server.explicit_configuration = Configuration({
        "services": {"search": {"transport": "http_json", "endpoint": "https://service.example/search"}},
        "capabilities": {"search": {"service": "search"}}}, connector_factory=FixtureConnector)
    response = app.session.post(app.url + "/api/actions", json={"action": "search-web", "query": "公开资料", "limit": 2})
    job = terminal(app, response.json()["id"])
    assert job["status"] == "complete"
    assert job["result"]["results"][0]["url"] == "https://example.org/result"
    assert observed == [{"query": "公开资料", "limit": 2}]
    assert app.session.get(app.url + "/api/settings").json()["explicit_providers_override"] is True
    assert app.session.post(app.url + "/api/actions", json={"action": "settings-providers", "config": {"services": {}, "capabilities": {}}}).status_code == 400


def test_action_restart_marks_interrupted(tmp_path):
    store = Store(tmp_path)
    first = create_server(store, port=0)
    with sqlite3.connect(store.root / "inbox.sqlite3") as db:
        db.execute("INSERT INTO action_jobs VALUES ('old','analyze','{}','running',NULL,NULL,'today',NULL)")
    first.server_close()
    second = create_server(store, port=0)
    try:
        assert second.workbench.job("old")["status"] == "failed"
        assert second.workbench.job("old")["error"]["code"] == "interrupted"
    finally:
        second.server_close()


def test_wiki_needs_review_preserves_candidate_result(app):
    output = {'status': 'needs_review', 'candidate': {'id': 'candidate-123'}}
    with patch('knowledge_capture.wiki.Wiki.build', return_value=output):
        response = app.session.post(app.url + '/api/actions', json={
            'action': 'wiki-build', 'identifier': 'interest_' + 'a' * 24})
        assert response.status_code == 202
        job = terminal(app, response.json()['id'])
    assert job['status'] == 'needs_review'
    assert job['result'] == output
    assert job['error'] is None


def test_settings_model_response_wait_api(app):
    with patch.dict('os.environ', {}, clear=True):
        response = app.session.post(app.url+'/api/actions',json={'action':'settings-model','base_url':'https://model.example/v1','model':'model','api_key':'private','timeout_seconds':240})
        assert response.status_code == 200
        assert response.json()['result']['model']['timeout_seconds'] == 240
        invalid = app.session.post(app.url+'/api/actions',json={'action':'settings-model','base_url':'https://model.example/v1','model':'model','timeout_seconds':601})
        assert invalid.status_code == 400
        assert app.session.get(app.url+'/api/settings').json()['model']['timeout_seconds'] == 240


def test_authenticated_record_download_accepts_record_and_source_versions(app):
    import io
    import zipfile
    from knowledge_capture.knowledge_records import KnowledgeRecords
    source=ingest(app.store,image=True)
    record=KnowledgeRecords(app.store).revise(source['source_id'],source['version_id'],markdown='保存原文的独立修订。')
    base=app.url+'/api/export/record/'+source['source_id']
    assert app.session.get(base+'/'+record['version_id'],headers={'Authorization':None}).status_code==401
    for version in [source['version_id'],record['version_id']]:
        response=app.session.get(base+'/'+version)
        assert response.status_code==200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert '知识记录.md' in archive.namelist()
            assert any(name.endswith('assets/frame.png') for name in archive.namelist())
    assert app.session.get(base+'/'+'a'*25).status_code==400


def test_vault_overview_sanitizes_sidecar_and_retry_is_authenticated(app,monkeypatch):
    private='/private/never-public/secret-home'
    (app.store.root/'.vault-sync-status.json').write_text(json.dumps({'status':'failed','path':private,'error':private+' denied','conflicts':[private+'/file.md']}))
    public=app.session.get(app.url+'/api/overview').json()['vault_sync']
    assert public['status']=='failed' and '未同步' in public['message']
    assert public['conflict_count']==1
    assert set(public)=={'status','message','conflict_count','broken_link_count'}
    assert private not in json.dumps(public)
    assert app.session.post(app.url+'/api/actions',headers={'Authorization':None},json={'action':'vault-sync'}).status_code==401
    submitted=app.session.post(app.url+'/api/actions',json={'action':'vault-sync'})
    assert submitted.status_code==202
    completed=terminal(app,submitted.json()['id'])
    assert completed['status']=='complete'
    assert completed['result']['status']=='complete'
    assert 'path' not in completed['result']
    assert app.session.get(app.url+'/api/overview').json()['vault_sync']['status']=='complete'


def test_vault_retry_failure_does_not_revert_saved_knowledge_or_expose_error_path(app,monkeypatch):
    from knowledge_capture.vault_export import VaultExportError
    item=ingest(app.store)
    def failed(*args,**kwargs):raise VaultExportError('/private/secret-path: disk failure')
    monkeypatch.setattr('knowledge_capture.vault_export.sync_vault',failed)
    submitted=app.session.post(app.url+'/api/actions',json={'action':'vault-sync'}).json()
    job=terminal(app,submitted['id'])
    assert job['status']=='failed' and job['error']['code']=='vault_sync_failed'
    assert '/private' not in json.dumps(job)
    assert app.store.read(item['source_id'])['metadata']['version_id']==item['version_id']
    public=app.session.get(app.url+'/api/overview').json()['vault_sync']
    assert public['status']=='failed' and '主知识库已保存' in public['message']
    assert '/private' not in json.dumps(public)
