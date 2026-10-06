"""Authenticated workbench operations, separated from capture ingress."""
import hashlib
import json
import mimetypes
import os
import re
import stat
import uuid
from contextlib import closing

from .store import now


class WorkbenchError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


def safe_error(exc):
    from .capture import CaptureError
    from .llm import LLMError
    from .processing import AnalysisError
    from .wiki import WikiError
    from .context_alerts import ContextError
    from .search import SearchError
    from .settings import SettingsError
    from .evidence_api import EvidenceError
    known = isinstance(exc, (WorkbenchError, CaptureError, LLMError, AnalysisError, WikiError, ContextError, SearchError, SettingsError, EvidenceError))
    return {"code": exc.code, "message": str(exc)} if known else {"code": "operation_failed", "message": "操作未完成，请检查配置或任务记录；已有原文保留。"}


class Workbench:
    def __init__(self, server, configuration=None):
        self.server, self.store = server, server.store
        with closing(server.database()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS action_jobs (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT)''')
            db.execute("UPDATE action_jobs SET status='failed',error=?,finished_at=? WHERE status IN ('queued','running')",
                       (json.dumps({"code": "interrupted", "message": "上次服务中断，操作结果需核实，未自动重试。"}, ensure_ascii=False), now()))

    def job(self, identifier):
        with closing(self.server.database()) as db:
            row = db.execute("SELECT * FROM action_jobs WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise WorkbenchError("not_found", "任务不存在。", 404)
        return {"id": row["id"], "action": row["action"], "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": json.loads(row["error"]) if row["error"] else None}

    @staticmethod
    def _vault_public(value):
        status = value.get('status') if isinstance(value, dict) else 'failed'
        if status not in {'complete','needs_review','failed','not_synced'}:
            status = 'failed'
        def count(field, fallback):
            items = value.get(field) if isinstance(value,dict) else None
            amount = len(items) if isinstance(items,list) else value.get(fallback,0) if isinstance(value,dict) else 0
            return min(1000000,max(0,amount)) if type(amount) is int else 0
        conflicts, broken = count('conflicts','conflict_count'),count('broken_links','broken_link_count')
        messages = {
            'complete':'Obsidian阅读镜像已同步。这里的编辑不会自动写回主知识库。',
            'not_synced':'尚未生成Obsidian阅读镜像。可现在生成，后续知识更新会自动同步。',
            'failed':'Obsidian阅读镜像未同步；主知识库已保存。请重试同步。',
            'needs_review':f'镜像需要复核：{conflicts}个编辑冲突、{broken}个链接待检查。人工修改已保留，请查看镜像同步报告。',
        }
        return {'status':status,'message':messages[status],'conflict_count':conflicts,'broken_link_count':broken}

    def vault_status(self):
        path = self.store.root / '.vault-sync-status.json'
        try:
            if path.is_symlink():
                return self._vault_public({'status':'failed'})
            if not path.exists():
                return self._vault_public({'status':'not_synced'})
            if path.stat().st_size > 2*1024*1024:
                raise ValueError()
            return self._vault_public(json.loads(path.read_text(encoding='utf-8')))
        except (OSError,ValueError,TypeError):
            return self._vault_public({'status':'failed'})

    def overview(self):
        from .processing import Processor
        from .wiki import Wiki
        from .context_alerts import ContextAlerts
        from .scheduler import Scheduler
        processor, alerts = Processor(self.store), ContextAlerts(self.store)
        sections = {"sources": self.store.list_sources, "captures": self.store.captures,
                    "analyses": processor.history, "interests": processor.interests,
                    "wikis": lambda: Wiki(self.store).list_pages(), "alerts": alerts.list_alerts,
                    "facts": alerts.list_facts, "schedules": lambda: Scheduler(self.store, self.server.configuration()).list()}
        result, errors, totals, has_more = {}, {}, {}, {}
        for key, read in sections.items():
            try:
                items = read()
                totals[key], has_more[key] = len(items), len(items) > 100
                result[key] = items[:100]
            except Exception as exc:
                result[key] = None
                errors[key] = safe_error(exc)
        with closing(self.server.database()) as db:
            result["inbox_jobs"] = [self.server.job(row[0]) for row in db.execute("SELECT id FROM inbox_jobs ORDER BY created_at DESC LIMIT 100")]
            result["action_jobs"] = [self.job(row[0]) for row in db.execute("SELECT id FROM action_jobs ORDER BY created_at DESC LIMIT 100")]
        configuration = self.server.configuration()
        services = configuration.data.get("services", {}) if configuration else {}
        capabilities = configuration.data.get("capabilities", {}) if configuration else {}
        result["capabilities"] = {
            "llm_configured": all(bool(os.environ.get(name)) for name in ("KC_LLM_BASE_URL", "KC_LLM_MODEL", "KC_LLM_API_KEY")),
            "auto_process": self.server.effective_auto_process(), "auto_process_override": self.server.auto_process,
            "configuration_loaded": configuration is not None,
            "scheduler_running": self.server.scheduler_status()["running"],
            "scheduler": self.server.scheduler_status(),
            "explicit_providers_override": self.server.explicit_configuration is not None,
            "services": [{"name": name, "transport": value.get("transport"),
                          "credentials_configured": all(bool(os.environ.get(env)) for env in
                              ([value["token_env"]] if value.get("token_env") else []) + list(value.get("headers_env", {}).values()))}
                         for name, value in services.items() if isinstance(value, dict)],
            "names": list(capabilities),
        }
        result["errors"] = errors
        result["vault_sync"] = self.vault_status()
        from .settings import Settings
        result["settings"] = Settings(self.store.root).public_view()
        result["capabilities"]["llm_configured"] = result["settings"]["model"]["configured"]
        if self.server.explicit_configuration is None:
            for service in result["capabilities"]["services"]:
                credential = result["settings"]["provider_credentials"].get(service["name"], {})
                service["credentials_configured"] = (not credential.get("token_env") or credential.get("token_configured", False)) and all(
                    value["configured"] for value in credential.get("headers_env", {}).values())
        result["settings"]["explicit_providers_override"] = self.server.explicit_configuration is not None
        result["limit"] = 100
        result["totals"], result["has_more"] = totals, has_more
        return result

    def source(self, identifier):
        from .evidence_api import read_source
        return read_source(self.store, identifier)

    def action(self, payload):
        fields = {
            "vault-sync": set(),
            "analyze": {"identifier"}, "wiki-build": {"identifier"}, "discover": {"identifier"},
            "search-web": {"query", "limit"},
            "alerts-check": {"source_ids"}, "interest-set": {"identifier", "state"},
            "context-set": {"subject", "field", "value", "status", "confirmed_at", "valid_days"},
            "alerts-set": {"identifier", "state"}, "schedule-set": {"identifier", "interval_hours", "enabled"},
            "settings-model": {"base_url", "model", "api_key", "clear_key", "timeout_seconds"},
            "settings-providers": {"config"}, "settings-provider-secret": {"variable", "value", "clear"},
            "settings-preferences": {"auto_process"},
            "settings-engine": {"kind", "transport", "endpoint", "api_key"},
            "settings-engine-discover": {"kind"},
            "settings-retrieval": {"kind", "endpoint", "model", "api_key", "enabled", "clear_key"},
        }
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            raise WorkbenchError("invalid_input", "操作参数必须包含有效 action。")
        action = payload["action"]
        if action not in fields or set(payload) - fields[action] - {"action"}:
            raise WorkbenchError("invalid_input", "操作或参数不在许可列表中。")
        args = {key: value for key, value in payload.items() if key != "action"}
        if "identifier" in fields[action]:
            expression = r"[a-f0-9]{24}" if action == "analyze" else r"[a-f0-9]{32}" if action == "alerts-set" else r"interest_[a-f0-9]{24}"
            if not isinstance(args.get("identifier"), str) or not re.fullmatch(expression, args["identifier"]):
                raise WorkbenchError("invalid_input", "操作对象标识无效。")
        if action == "alerts-check" and "source_ids" in args:
            if (not isinstance(args["source_ids"], list) or not 1 <= len(args["source_ids"]) <= 12
                    or any(not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{24}", sid) for sid in args["source_ids"])):
                raise WorkbenchError("invalid_input", "关联分析需提供 1 至 12 个有效来源标识。")
        if action == "search-web" and (not isinstance(args.get("query"), str) or not args["query"].strip()
                or len(args["query"]) > 600 or type(args.get("limit", 5)) is not int or not 1 <= args.get("limit", 5) <= 20):
            raise WorkbenchError("invalid_input", "请输入有效搜索词，结果数量为 1 至 20。")
        if action in {"vault-sync", "analyze", "wiki-build", "discover", "alerts-check", "search-web", "settings-engine-discover"}:
            identifier = uuid.uuid4().hex
            with closing(self.server.database()) as db, db:
                db.execute("INSERT INTO action_jobs VALUES (?,?,?,'queued',NULL,NULL,?,NULL)",
                           (identifier, action, json.dumps(args, ensure_ascii=False), now()))
            self.server.executor.submit(self._run, identifier, action, args)
            return 202, self.job(identifier)
        try:
            return 200, {"result": self._execute(action, args)}
        except Exception as exc:
            detail = safe_error(exc)
            raise WorkbenchError(detail["code"], detail["message"]) from None

    def _execute(self, action, args):
        from .processing import Processor
        from .wiki import Wiki
        from .context_alerts import ContextAlerts
        from .discovery import Discovery
        from .scheduler import Scheduler
        from .settings import Settings
        identifier = args.get("identifier")
        if action == "vault-sync":
            from .vault_export import sync_after_commit
            result = self._vault_public(sync_after_commit(self.store))
            if result['status']=='failed':
                raise WorkbenchError('vault_sync_failed',result['message'])
            return result
        if action == "analyze":
            return Processor(self.store).analyze(identifier)
        if action == "wiki-build":
            return Wiki(self.store).build(identifier)
        if action == "discover":
            return Discovery(self.store, self.server.configuration()).run(identifier)
        if action == "search-web":
            from .providers import ConfiguredSearch
            configuration = self.server.configuration()
            if configuration is None:
                raise WorkbenchError("configuration_missing", "请先在设置中配置搜索服务。")
            return {"status": "complete", "query": args["query"],
                    "results": ConfiguredSearch(configuration, store=self.store).search(args["query"], args.get("limit", 5))}
        if action == "alerts-check":
            return ContextAlerts(self.store).analyze(args.get("source_ids"))
        if action == "interest-set":
            return Processor(self.store).feedback(identifier, args.get("state"))
        if action == "context-set":
            return ContextAlerts(self.store).set_fact(**args)
        if action == "alerts-set":
            return ContextAlerts(self.store).feedback(identifier, args.get("state"))
        if action == "settings-retrieval":
            return Settings(self.store.root).save_retrieval(**args)
        if action == "settings-model":
            return Settings(self.store.root).save_model(**args)
        if action == "settings-preferences":
            return Settings(self.store.root).save_preferences(**args)
        if action in {"settings-engine", "settings-engine-discover"}:
            if self.server.explicit_configuration is not None:
                raise WorkbenchError("configuration_override", "当前服务使用启动时明确指定的配置，界面不能覆盖。")
            settings = Settings(self.store.root)
            if action == "settings-engine":
                return settings.save_engine(**args)
            view = settings.discover_engine(**args)
            adapted = view.get('engines', {}).get(args.get('kind'), {}).get('status') == 'adapted'
            return {**view, 'status': 'complete' if adapted else 'partial'}
        if action == "settings-providers":
            if self.server.explicit_configuration is not None:
                raise WorkbenchError("configuration_override", "当前服务使用启动时明确指定的配置，界面不能覆盖。")
            return Settings(self.store.root).save_providers(**args)
        if action == "settings-provider-secret":
            return Settings(self.store.root).save_provider_secret(**args)
        return Scheduler(self.store, self.server.configuration()).set(identifier, args.get("interval_hours", 24), args.get("enabled", True))

    def _run(self, identifier, action, args):
        with closing(self.server.database()) as db, db:
            db.execute("UPDATE action_jobs SET status='running' WHERE id=?", (identifier,))
        result, error = None, None
        try:
            output = self._execute(action, args)
            status = output.get("status", "complete")
            if status not in {"complete", "partial", "no_new", "no_results", "needs_review"}:
                raise ValueError()
            result = json.dumps(output, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            status = "failed"
            error = json.dumps(safe_error(exc), ensure_ascii=False)
        with closing(self.server.database()) as db, db:
            db.execute("UPDATE action_jobs SET status=?,result=?,error=?,finished_at=? WHERE id=?", (status, result, error, now(), identifier))

    def asset(self, sid, version, filename):
        document = self.store.read(sid, version)
        relative = "assets/" + filename
        asset = next((item for item in document["metadata"].get("assets", [])
                      if item.get("relative_path") == relative and item.get("status") == "complete"), None)
        if not asset:
            raise WorkbenchError("asset_missing", "图片不在来源登记记录中。", 404)
        path = self.store.root / "sources" / sid / "versions" / version / "assets" / filename
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise WorkbenchError("asset_invalid", "图片路径不安全。")
            if parent == self.store.root:
                break
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError()
                data = source.read(12 * 1024 * 1024 + 1)
            if len(data) > 12 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError()
        except (OSError, ValueError):
            raise WorkbenchError("asset_invalid", "图片缺失或完整性校验失败。") from None
        mime = mimetypes.guess_type(filename)[0]
        if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise WorkbenchError("asset_invalid", "工作台不展示该图片格式。")
        return data, mime
