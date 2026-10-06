"""Authenticated, loopback-only capture inbox for browser and user-owned bridges."""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import threading
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from .llm import _strict_json
from .capture import CaptureError
from .store import canonical_url, now

BODY_LIMIT = 64 * 1024
EXTENSION = re.compile(r"chrome-extension://[a-p]{32}\Z")


def _root_lock(root):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root / ".api-lock", flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        raise ValueError("该知识库已有采集服务运行，请使用原服务或更换数据目录") from None


def _token(root):
    path = root / ".api-token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("采集令牌文件必须是普通文件")
    else:
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_urlsafe(32))
    os.chmod(path, 0o600)
    value = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise ValueError("采集令牌文件格式无效")
    return value


def _payload(value, inbox=False):
    allowed = {"url", "note", "origin", "idempotency_key"} | ({"text"} if inbox else set())
    if not isinstance(value, dict) or set(value) - allowed or value.get("origin", "user") != "user":
        raise ValueError()
    note, key = value.get("note", ""), value.get("idempotency_key")
    if not isinstance(note, str) or len(note) > 8192 or (key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128)):
        raise ValueError()
    url = value.get("url")
    if "text" in value:
        if url is not None or not isinstance(value["text"], str):
            raise ValueError()
        links = set(re.findall(r'https?://[^\s<>"\u3000，。；！？【】《》]+', value["text"]))
        links = {link.rstrip(".,;!?)）]") for link in links}
        if len(links) != 1:
            raise ValueError()
        url = links.pop()
    if not isinstance(url, str) or len(url) > 16384 or any(ord(c) <= 32 for c in url):
        raise ValueError()
    parsed = urlsplit(url)
    parsed.port
    if parsed.username is not None or parsed.password is not None:
        raise ValueError()
    return {"url": canonical_url(url), "note": note, "origin": "user"}, key


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def effective_auto_process(self):
        from .settings import Settings
        settings = Settings(self.store)
        if self.auto_process or settings.preferences()['auto_process']:
            return True
        if self.explicit_configuration is not None:
            return False
        view = settings.public_view()
        return (view.get('engines', {}).get('reader', {}).get('status') == 'adapted'
                and view['model']['configured'])

    def scheduler_status(self):
        with self._scheduler_guard:
            return {"running": bool(self._scheduler_thread and self._scheduler_thread.is_alive()),
                    "state": self._scheduler_state, "error": self._scheduler_error,
                    "last_checked_at": self._scheduler_checked}

    def _scheduler_loop(self):
        try:
            from .scheduler import Scheduler
            while not self._scheduler_stop.is_set():
                scheduler = Scheduler(self.store, self.configuration())
                # Disabled plans make no outbound calls, but orphaned runs still
                # need local reconciliation after an interrupted service.
                has_work = (any(plan["enabled"] for plan in scheduler.list())
                            or any(run["status"] == "running" for run in scheduler.history()))
                result = scheduler.tick() if has_work else {"status": "idle"}
                with self._scheduler_guard:
                    self._scheduler_checked = now()
                    if result["status"] == "needs_review":
                        self._scheduler_state = "needs_review"
                        self._scheduler_error = {"code": "schedule_needs_review", "message": "定时任务存在失败、部分完成或中断，请检查计划运行记录后手动恢复。"}
                    elif self._scheduler_state != "needs_review" or result["status"] == "processed":
                        self._scheduler_state = "idle" if result["status"] == "idle" else "running"
                        self._scheduler_error = None
                self._scheduler_stop.wait(self.scheduler_poll_seconds)
        except Exception:
            with self._scheduler_guard:
                self._scheduler_state = "failed"
                self._scheduler_error = {"code": "scheduler_stopped", "message": "定时工作线程异常停止，未自动重试；请检查配置和任务记录后重新启动服务。"}
                self._scheduler_checked = now()

    def serve_forever(self, poll_interval=0.5):
        with self._scheduler_guard:
            if self._scheduler_thread is None:
                self._scheduler_state = "starting"
                self._scheduler_thread = threading.Thread(target=self._scheduler_loop, name="knowledge-scheduler", daemon=True)
                self._scheduler_thread.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._stop_scheduler()

    def _stop_scheduler(self):
        if not hasattr(self, "_scheduler_stop"):
            return
        self._scheduler_stop.set()
        thread = self._scheduler_thread
        if thread and thread is not threading.current_thread():
            thread.join()
        with self._scheduler_guard:
            if self._scheduler_state not in {"failed", "needs_review"}:
                self._scheduler_state = "stopped"

    def shutdown(self):
        super().shutdown()
        self._stop_scheduler()

    def configuration(self):
        from .settings import Settings
        return Settings(self.store.root).provider_configuration(self.explicit_configuration)

    def database(self):
        db = sqlite3.connect(self.jobs_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, payload, key):
        packed = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(packed.encode()).hexdigest()
        with closing(self.database()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                previous = db.execute("SELECT * FROM inbox_jobs WHERE idempotency_key=?", (key,)).fetchone()
                if previous:
                    if previous["payload_hash"] != digest:
                        raise ValueError("conflict")
                    return self.job(previous["id"])
            identifier = uuid.uuid4().hex
            db.execute("INSERT INTO inbox_jobs VALUES (?, ?, ?, ?, 'queued', NULL, NULL, ?, NULL)",
                       (identifier, key, digest, packed, now()))
        self.executor.submit(self.run_job, identifier, payload)
        return self.job(identifier)

    def knowledge_replay(self, key, request):
        from .knowledge_records import KnowledgeRecordError
        with closing(self.database()) as db:
            row = db.execute('SELECT id,payload FROM inbox_jobs WHERE idempotency_key=?', (key,)).fetchone()
        if not row:
            return None
        if json.loads(row['payload']).get('_knowledge_request') != request:
            raise KnowledgeRecordError('idempotency_conflict', '幂等标识冲突。')
        return self.job(row['id'])

    def job(self, identifier):
        with closing(self.database()) as db:
            row = db.execute("SELECT * FROM inbox_jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row['payload'])
        return {"id": row["id"], "status": row["status"],
                "url": payload.get('url'), "note": payload.get('note', ''),
                "created_at": row['created_at'], "finished_at": row['finished_at'],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": json.loads(row["error"]) if row["error"] else None}

    def run_job(self, identifier, payload):
        with closing(self.database()) as db, db:
            db.execute("UPDATE inbox_jobs SET status='running' WHERE id=?", (identifier,))
        result, error = None, None
        try:
            args = {k:v for k,v in payload.items() if k != "_knowledge_request"}
            configuration = self.configuration()
            if configuration is not None:
                from .providers import CaptureRouter
                args["capture_fn"] = CaptureRouter(configuration, store=self.store).capture
            result = self.store.ingest(**args)
            status = result.get("status")
            if status not in {"complete", "partial"}:
                raise ValueError()
            if self.effective_auto_process():
                try:
                    result = self.process_capture(result)
                except Exception:
                    result = {**result, "capture": dict(result), "status": "partial",
                              "analysis": {"status": "failed", "error": {"code": "processing_failed", "message": "自动整理未完成，原文保留。"}},
                              "wiki": {"status": "skipped", "reason": "自动整理中断"},
                              "alerts": {"status": "skipped", "reason": "自动整理中断"},
                              "warnings": list(result.get("warnings", [])) + ["自动整理中断，已采集原文保留，未自动重试。"]}
                status = result["status"]
            result = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            status, result = "failed", None
            detail = ({"code": exc.code, "message": str(exc)} if isinstance(exc, CaptureError)
                      else {"code": "capture_failed", "message": "采集失败，请检查来源或连接器配置后重试。"})
            if getattr(exc, 'code', None) == 'version_conflict':
                detail = {'code':'version_conflict','message':'知识已更新，重新读取后再刷新。','current_version':getattr(exc,'current_version',None)}
            error = json.dumps(detail, ensure_ascii=False)
        with closing(self.database()) as db, db:
            db.execute("UPDATE inbox_jobs SET status=?,result=?,error=?,finished_at=? WHERE id=?",
                       (status, result, error, now(), identifier))

    def process_capture(self, capture):
        from .llm import CloudClient, LLMError
        from .processing import Processor, AnalysisError
        from .wiki import Wiki, WikiError, normalize
        from .context_alerts import ContextAlerts, ContextError

        result = {**capture, "capture": dict(capture), "warnings": list(capture.get("warnings", [])),
                  "analysis": {"status": "skipped", "reason": "尚未分析"},
                  "wiki": {"status": "skipped", "reason": "尚未分析", "pages": [], "skipped_topic_ids": []},
                  "alerts": {"status": "skipped", "reason": "尚未检查背景事实"}}

        def failure(stage, exc):
            known = isinstance(exc, (LLMError, AnalysisError, WikiError, ContextError))
            detail = {"code": exc.code, "message": str(exc)} if known else {
                "code": "processing_failed", "message": "后续处理失败，已采集原文保留。"}
            result[stage].update(status="failed", error=detail)
            result[stage].pop("reason", None)
            result["status"] = "partial"
            result["warnings"].append(f"{stage}：{detail['message']}")

        client = None
        try:
            client = CloudClient.for_store(self.store)
            processor = Processor(self.store)
            if self.store.read(capture["source_id"])["metadata"]["version_id"] != capture["version_id"]:
                raise AnalysisError("source_changed", "来源已有新版本，本次原文保留，未对不同版本自动整理。")
            analysis = processor.analyze(capture["source_id"], client=client)
            if analysis["source_version"] != capture["version_id"]:
                raise AnalysisError("source_changed", "分析期间来源版本变化，本次原文保留，需重新整理。")
            result["analysis"] = {"status": analysis["status"], "result": analysis}
            result['interest_inference'] = analysis.get('interest_inference', {'status': 'skipped'})
            if result['interest_inference']['status'] == 'failed':
                result['status'] = 'partial'
                result['warnings'].append('跨资料兴趣归纳未完成，已完成的原文与整理保留。')
            if analysis["status"] != "complete":
                result["status"] = "partial"
                result["warnings"].append("AI 整理仅部分完成，原文已保留。")
        except Exception as exc:
            failure("analysis", exc)
            result["wiki"]["reason"] = "本次分析失败，未自动更新 Wiki"
        else:
            result["wiki"] = {"status": "complete", "pages": [], "skipped_topic_ids": []}
            try:
                names = {normalize(item["name"]) for item in processor.read(analysis["id"])["record"]["topics"]}
                topics = [topic for topic in processor.interests() if normalize(topic["name"]) in names
                          or (topic.get('inference_ids') and any(e['source_id'] == capture['source_id'] for e in topic['evidence']))]
                topics.sort(key=lambda topic: (topic.get("state") != "followed",
                                                -topic.get("user_source_count", 0), topic["id"]))
                wiki = Wiki(self.store)
                for topic in topics[:3]:
                    try:
                        page = wiki.build(topic["id"], client=client)
                        result["wiki"]["pages"].append(page)
                        if page["status"] != "complete":
                            result["wiki"]["status"] = result["status"] = "partial"
                            result["warnings"].append("部分 Wiki 页面未完整更新，已采集原文保留。")
                    except Exception as exc:
                        failure("wiki", exc)
                        result["wiki"]["pages"].append({"topic_id": topic["id"], "status": "failed",
                                                        "error": result["wiki"]["error"]})
                if len(topics) > 3:
                    result["wiki"]["skipped_topic_ids"] = [topic["id"] for topic in topics[3:]]
                    result["wiki"]["status"] = result["status"] = "partial"
                    result["warnings"].append("本次最多自动更新 3 个主题，其余主题需另行更新。")
                if not topics:
                    result["wiki"]["status"] = "skipped"
                    result["wiki"]["reason"] = "本次资料没有可更新的主题"
            except Exception as exc:
                failure("wiki", exc)

        try:
            alerts = ContextAlerts(self.store)
            if not any(fact["status"] == "confirmed" for fact in alerts.list_facts()):
                result["alerts"] = {"status": "skipped", "reason": "没有当前有效的已确认背景事实"}
            elif client is None:
                result["alerts"] = {"status": "skipped", "reason": "模型未配置，未执行关联分析"}
            else:
                if self.store.read(capture["source_id"])["metadata"]["version_id"] != capture["version_id"]:
                    raise ContextError("source_changed", "来源版本变化，未对不同版本自动生成关联提醒。")
                output = alerts.analyze([capture["source_id"]], client=client)
                result["alerts"] = {"status": output["status"], "result": output}
                if output["status"] != "complete":
                    result["status"] = "partial"
                    result["warnings"].append("关联提醒分析未完整完成，原文已保留。")
        except Exception as exc:
            failure("alerts", exc)
        return result

    def server_close(self):
        self._stop_scheduler()
        super().server_close()
        try:
            if hasattr(self, "executor"):
                self.executor.shutdown(wait=True)
        finally:
            if getattr(self, "_root_lock_fd", None) is not None:
                os.close(self._root_lock_fd)
                self._root_lock_fd = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_data(status, raw, "application/json; charset=utf-8")

    def send_data(self, status, raw, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        origin = self.headers.get("Origin", "")
        if EXTENSION.fullmatch(origin) or origin == "http://" + self.headers.get("Host", ""):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if self.command == "OPTIONS":
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Idempotency-Key")
        self.end_headers()
        self.wfile.write(raw)

    def gate(self, auth=True):
        try:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                raise ValueError()
            parsed = urlsplit("http://" + hosts[0])
            if (parsed.port not in {self.server.server_port, self.server.public_port} or parsed.path or parsed.query or parsed.fragment
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError()
            if parsed.hostname != "localhost" and not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError()
        except (ValueError, TypeError):
            self.reply(403, {"error": "不允许的 Host"})
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and not EXTENSION.fullmatch(origins[0]) and origins[0] != "http://" + hosts[0]):
            self.reply(403, {"error": "不允许的跨域来源"})
            return False
        if auth:
            provided = self.headers.get_all("Authorization", [])
            if len(provided) != 1 or not hmac.compare_digest(provided[0].encode(), ("Bearer " + self.server._token).encode()):
                self.reply(401, {"error": "需要有效采集令牌"})
                return False
        return True

    def do_OPTIONS(self):
        if self.gate(auth=False):
            headers = {x.strip().lower() for x in self.headers.get("Access-Control-Request-Headers", "").split(",") if x.strip()}
            if not headers <= {"content-type", "authorization", "idempotency-key"} or self.headers.get("Access-Control-Request-Method", "GET") not in {"GET", "POST"}:
                self.reply(403, {"error": "不允许的预检请求"})
            else:
                self.reply(200, {"status": "ok"})

    def do_GET(self):
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/renderer.js": ("renderer.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if not self.gate(auth=self.path != "/api/health" and self.path not in static):
            return
        if self.path in static:
            filename, mime = static[self.path]
            path = Path(__file__).parent / "web" / filename
            if not path.is_file() or path.is_symlink():
                self.reply(404, {"error": "工作台文件尚未就绪"})
                return
            self.send_data(200, path.read_bytes(), mime)
            return
        if self.path.startswith("/api/v1/"):
            from .knowledge_api import KnowledgeAPI, failure
            try:
                status, result = KnowledgeAPI(self.server.store, self.server.enqueue, self.server.job, self.server.knowledge_replay).get(self.path)
            except Exception as exc:
                status, result = failure(exc)
            self.reply(status, result)
            return
        if self.workbench_get():
            return
        if self.path == "/api/health":
            self.reply(200, {"status": "ok"})
        elif re.fullmatch(r"/api/captures/[a-f0-9]{32}", self.path):
            job = self.server.job(self.path.rsplit("/", 1)[1])
            self.reply(200 if job else 404, job or {"error": "任务不存在"})
        else:
            self.reply(404, {"error": "接口不存在"})

    def workbench_get(self):
        from .workbench_api import WorkbenchError, safe_error
        api = self.server.workbench
        try:
            if self.path.startswith('/api/export/'):
                from .workbench_exports import prepare_export, ExportError
                from .portable import PortableError
                match = re.fullmatch(r'/api/export/(library|source|analysis|wiki|record)(?:/([a-z0-9_]+))?(?:/((?:[a-f0-9]{24}|[a-f0-9]{32})))?', self.path)
                if not match:
                    raise WorkbenchError('invalid_input', '导出地址无效。')
                try:
                    with prepare_export(self.server.store, match[1], match[2], version=match[3]) as download:
                        self.send_response(200)
                        self.send_header('Content-Type', download.content_type)
                        self.send_header('Content-Length', str(download.size))
                        self.send_header('Content-Disposition', f'attachment; filename="{download.filename}"')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        with download.path.open('rb') as stream:
                            for block in iter(lambda: stream.read(64 * 1024), b''):
                                self.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                except (ExportError, PortableError) as exc:
                    raise WorkbenchError('export_failed', str(exc)) from None
                return True
            elif self.path == "/api/overview":
                output = api.overview()
            elif urlsplit(self.path).path == "/api/search":
                try:
                    raw_query = urlsplit(self.path).query
                    if re.search(r"%(?![0-9A-Fa-f]{2})", raw_query):
                        raise ValueError()
                    query = parse_qs(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=2, errors="strict")
                    if set(query) - {"q", "limit"} or "q" not in query or any(len(v) != 1 for v in query.values()):
                        raise ValueError()
                    term, limit = query["q"][0], int(query.get("limit", ["20"])[0])
                    if not term.strip() or len(term) > 600 or not 1 <= limit <= 100:
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise WorkbenchError("invalid_input", "请输入有效关键词，结果数量为 1 至 100。") from None
                output = {"query": term, "limit": limit, "results": self.server.store.search(term, limit=limit)}
            elif self.path == "/api/settings":
                from .settings import Settings
                output = Settings(self.server.store.root).public_view()
                output["explicit_providers_override"] = self.server.explicit_configuration is not None
            elif match := re.fullmatch(r"/api/sources/([a-f0-9]{24})", self.path):
                output = api.source(match[1])
            elif match := re.fullmatch(r"/api/sources/([a-f0-9]{24})/versions/([a-f0-9]{24})", self.path):
                from .evidence_api import read_source
                output = read_source(self.server.store, *match.groups())
            elif match := re.fullmatch(r"/api/analyses/([a-f0-9]{32})", self.path):
                from .evidence_api import read_analysis
                output = read_analysis(self.server.store, match[1])
            elif match := re.fullmatch(r"/api/wiki/(interest_[a-f0-9]{24})", self.path):
                from .evidence_api import read_wiki
                output = read_wiki(self.server.store, match[1])
            elif match := re.fullmatch(r"/api/wiki/(interest_[a-f0-9]{24})/versions/([a-f0-9]{32})", self.path):
                from .evidence_api import read_wiki
                output = read_wiki(self.server.store, *match.groups())
            elif match := re.fullmatch(r"/api/alerts/([a-f0-9]{32})", self.path):
                from .evidence_api import read_alert
                output = read_alert(self.server.store, match[1])
            elif match := re.fullmatch(r"/api/actions/([a-f0-9]{32})", self.path):
                output = api.job(match[1])
            elif match := re.fullmatch(r"/api/assets/([a-f0-9]{24})/([a-f0-9]{24})/([A-Za-z0-9_.-]{1,200})", self.path):
                data, mime = api.asset(*match.groups())
                self.send_data(200, data, mime)
                return True
            else:
                return False
            self.reply(200, output)
        except Exception as exc:
            self.reply(exc.status if isinstance(exc, WorkbenchError) else 404 if isinstance(exc, FileNotFoundError) else 400,
                       {"error": safe_error(exc)})
        return True

    def do_POST(self):
        if not self.gate():
            return
        if self.path not in {"/api/captures", "/api/inbox", "/api/actions"} and not self.path.startswith("/api/v1/"):
            self.reply(404, {"error": "接口不存在"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
            self.reply(400, {"error": "请求必须提供有效长度"})
            return
        length = int(lengths[0])
        if length > BODY_LIMIT:
            self.reply(413, {"error": "请求超过大小限制"})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.reply(415, {"error": "请求必须使用 JSON"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            value = _strict_json(raw.decode("utf-8"))
            if self.path != "/api/actions" and not self.path.startswith("/api/v1/"):
                payload, key = _payload(value, inbox=self.path == "/api/inbox")
        except (ValueError, UnicodeError, RecursionError, OSError):
            self.reply(400, {"error": "请逐条提交一个有效网页链接与可选备注"})
            return
        if self.path.startswith("/api/v1/"):
            from .knowledge_api import KnowledgeAPI, failure
            try:
                keys = self.headers.get_all("Idempotency-Key", [])
                status, result = KnowledgeAPI(self.server.store, self.server.enqueue, self.server.job, self.server.knowledge_replay).post(self.path, value, keys[0] if len(keys) == 1 else None)
            except Exception as exc:
                status, result = failure(exc)
            self.reply(status, result)
            return
        if self.path == "/api/actions":
            from .workbench_api import WorkbenchError, safe_error
            try:
                status, output = self.server.workbench.action(value)
                self.reply(status, output)
            except Exception as exc:
                self.reply(exc.status if isinstance(exc, WorkbenchError) else 400, {"error": safe_error(exc)})
            return
        try:
            job = self.server.enqueue(payload, key)
        except ValueError:
            self.reply(409, {"error": "幂等标识已用于不同请求"})
            return
        self.reply(202, job)


def create_server(store, host="127.0.0.1", port=8765, configuration=None, auto_process=False, *, container_network=False, public_port=None):
    if public_port is not None and (not container_network or type(public_port) is not int or not 1 <= public_port <= 65535):
        raise ValueError("外部端口仅用于显式容器入口，且必须为 1 至 65535 的整数")
    if type(auto_process) is not bool:
        raise ValueError("auto_process 必须为布尔值")
    try:
        if type(container_network) is not bool:
            raise ValueError()
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback and not (container_network and host == "0.0.0.0"):
            raise ValueError()
    except ValueError:
        raise ValueError("采集服务只允许绑定本机 loopback 地址") from None
    server_class = _Server
    if ":" in host:
        class IPv6Server(_Server):
            address_family = socket.AF_INET6
        server_class = IPv6Server
    # Bind first: failed attempts must never mutate old jobs or token state.
    server = server_class((host, port), _Handler)
    try:
        # Hold an OS lock for this data directory until all workers have stopped.
        server._root_lock_fd = _root_lock(store.root)
        server.store = store
        server.public_port = public_port if public_port is not None else server.server_port
        server._scheduler_guard = threading.RLock()
        server._scheduler_stop = threading.Event()
        server._scheduler_thread = None
        server._scheduler_state = "not_started"
        server._scheduler_error = None
        server._scheduler_checked = None
        server.scheduler_poll_seconds = 5
        server.explicit_configuration = configuration
        server.auto_process = auto_process
        server._token = _token(store.root)
        server.jobs_path = store.root / "inbox.sqlite3"
        with closing(server.database()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbox_jobs (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                created_at TEXT NOT NULL, finished_at TEXT)""")
            db.execute("UPDATE inbox_jobs SET status='failed',error=?,finished_at=? WHERE status IN ('queued','running')",
                       (json.dumps({"code": "interrupted", "message": "上次服务中断，任务未完成，请重新提交。"}, ensure_ascii=False), now()))
        server.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture")
        from .workbench_api import Workbench
        server.workbench = Workbench(server, configuration)
        return server
    except Exception:
        server.server_close()
        raise
