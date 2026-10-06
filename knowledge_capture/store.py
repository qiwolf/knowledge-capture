"""Immutable source versions, capture history, and portable exports."""
from __future__ import annotations

import hashlib
import copy
import re
import json
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlsplit, urlunsplit


class VersionConflict(ValueError):
    code = 'version_conflict'

    def __init__(self, current_version):
        self.current_version = current_version
        super().__init__('知识或来源版本已改变，未覆盖新内容；请读取当前版本后重新提交。')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url: str) -> str:
    url = urldefrag(url.strip())[0]
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请输入完整的 http 或 https 网页链接")
    if parsed.username or parsed.password:
        raise ValueError("链接中不能包含账号密码")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def source_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:24]


def raw_evidence_files(metadata: dict, directory: Path) -> list[tuple[str, str]]:
    """Validate committed engine evidence before snapshots, reuse, or export."""
    normalization = metadata.get('acquisition', {}).get('normalization')
    if not normalization:
        return []
    try:
        files = normalization['raw_files']
        if not isinstance(files, dict) or not {'raw/response.txt', 'raw/inventory.json', 'raw/selection.json'} <= files.keys():
            raise ValueError()
        if len(files) > 131 or normalization['raw_path'] != 'raw/response.txt' or normalization['archive_path'] != 'raw':
            raise ValueError()
        if (directory / 'raw').is_symlink():
            raise ValueError()
        for name, digest in files.items():
            if not re.fullmatch(r'raw/(?:response\.txt|inventory\.json|selection(?:-[0-9]{4})?\.json)', name):
                raise ValueError()
            path = directory / name
            if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest) or path.is_symlink() or not path.is_file():
                raise ValueError()
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError()
        if files['raw/response.txt'] != normalization['raw_sha256']:
            raise ValueError()
        return sorted(files.items())
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError('引擎原始响应或选择记录完整性检查失败') from None


def copy_raw_evidence(metadata: dict, source: Path, destination: Path) -> None:
    files = raw_evidence_files(metadata, source)
    if files:
        (destination / 'raw').mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, _ in files:
            shutil.copy2(source / name, destination / name)
        raw_evidence_files(metadata, destination)


def verify_version(directory: Path, result: dict, asset_manifest: list) -> None:
    """Reuse only intact committed versions or intact leftovers from a crash."""
    try:
        saved = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        body = (directory / "content.md").read_text(encoding="utf-8").split("---\n\n", 1)[-1]
        if body != result["markdown"].strip() + "\n":
            raise ValueError()
        for key in ("title", "author", "published_at", "status", "warnings", "assets", "final_url", "acquisition"):
            if saved.get(key) != result.get(key):
                raise ValueError()
        raw_evidence_files(saved, directory)
        for relative, digest in asset_manifest:
            asset = directory / relative
            if asset.is_symlink() or hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
                raise ValueError()
    except (OSError, ValueError, KeyError):
        raise ValueError("已有版本完整性检查失败，保留原文件等待检查") from None


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite3"
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, latest_version TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    source_id TEXT NOT NULL, version_id TEXT NOT NULL,
                    path TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, version_id)
                );
                CREATE TABLE IF NOT EXISTS captures (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL,
                    source_id TEXT, version_id TEXT, status TEXT NOT NULL,
                    error_code TEXT, error TEXT, origin TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def begin_capture(self, url: str, note: str = "", origin: str = "user") -> str:
        if origin not in {"user", "discovery"}:
            raise ValueError("origin 必须为 user 或 discovery")
        url = canonical_url(url)
        capture_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO captures VALUES (?, ?, NULL, NULL, 'running', NULL, NULL, ?, ?, ?, NULL)",
                       (capture_id, url, origin, note, now()))
        return capture_id

    def fail_capture(self, capture_id: str, code: str, error: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE captures SET status='failed', error_code=?, error=?, finished_at=? WHERE id=? AND status='running'",
                       (code, error, now(), capture_id))

    def ingest(self, url: str, note: str = "", origin: str = "user", capture_fn=None, *, expected_source_version=None, expected_knowledge_version=None) -> dict:
        if capture_fn is None:
            from .capture import capture_url
            capture_fn = capture_url
        capture_id = self.begin_capture(url, note, origin)
        try:
            with tempfile.TemporaryDirectory(prefix=".capture-", dir=self.root) as work:
                assets = Path(work) / "assets"
                assets.mkdir()
                result = capture_fn(canonical_url(url), assets)
                return self.commit_capture(capture_id, result, assets, expected_source_version=expected_source_version, expected_knowledge_version=expected_knowledge_version)
        except Exception as exc:
            # Never persist arbitrary exception strings that could contain credentials.
            code = getattr(exc, "code", "processing_error")
            from .capture import CaptureError
            message = str(exc) if isinstance(exc, (CaptureError, ValueError)) else "处理失败，请检查连接或采集日志后重试"
            self.fail_capture(capture_id, code, message)
            raise

    def commit_capture(self, capture_id: str, result: dict, assets_dir: Path, *, expected_source_version=None, expected_knowledge_version=None) -> dict:
        if result.get("status") not in {"complete", "partial"}:
            raise ValueError("提取结果必须明确为 complete 或 partial")
        if not result.get("markdown", "").strip():
            raise ValueError("正文为空，不能保存为采集成功")
        if not result.get("title", "").strip():
            raise ValueError("缺少文档标题")
        result = copy.deepcopy(result)
        normalization = result.get('acquisition', {}).get('normalization')
        if normalization is not None:
            raw_dir = assets_dir.parent / 'raw'
            if not isinstance(normalization, dict) or raw_dir.is_symlink() or not raw_dir.is_dir():
                raise ValueError('引擎原始响应缺失，不能保存为采集成功')
            files = {}
            for path in raw_dir.iterdir():
                if re.fullmatch(r'(?:response\.txt|inventory\.json|selection(?:-[0-9]{4})?\.json)', path.name):
                    if path.is_symlink() or not path.is_file():
                        raise ValueError('引擎原始响应路径不合法')
                    files['raw/' + path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            normalization.update(raw_path='raw/response.txt', archive_path='raw', raw_files=files)
            raw_evidence_files(result, assets_dir.parent)
        asset_manifest = []
        for asset in result.get("assets", []):
            if asset.get("status") != "complete":
                if result["status"] != "partial":
                    raise ValueError("图片缺失必须标记部分完成")
                continue
            relative = Path(asset["relative_path"])
            if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "assets" or ".." in relative.parts:
                raise ValueError("图片路径必须位于文档 assets 目录")
            image = assets_dir / relative.name
            if image.is_symlink() or not image.is_file():
                raise ValueError("图片文件缺失或路径不合法")
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            if digest != asset["sha256"]:
                raise ValueError("图片校验不一致")
            asset_manifest.append((str(relative), digest))
        version_material = {key: result.get(key) for key in (
            "title", "markdown", "author", "published_at", "status", "warnings", "assets", "final_url"
        )}
        if "acquisition" in result:
            version_material["acquisition"] = result["acquisition"]
        version_id = hashlib.sha256(json.dumps(version_material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        timestamp = now()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            capture = db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
            if capture is None or capture["status"] != "running":
                raise ValueError("不存在可提交的采集任务")
            sid = source_id(capture["url"])
            if expected_source_version is not None or expected_knowledge_version is not None:
                source_head = db.execute('SELECT latest_version FROM sources WHERE id=?', (sid,)).fetchone()
                current_source = source_head['latest_version'] if source_head else None
                if expected_source_version is not None and expected_source_version != current_source:
                    raise VersionConflict(current_source)
                current_knowledge = current_source
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_records'").fetchone():
                    record_head = db.execute('SELECT latest_version FROM knowledge_records WHERE id=?', (sid,)).fetchone()
                    if record_head:
                        current_knowledge = record_head['latest_version']
                if expected_knowledge_version is not None and expected_knowledge_version != current_knowledge:
                    raise VersionConflict(current_knowledge)
            relative_dir = Path("sources") / sid / "versions" / version_id
            final_dir = self.root / relative_dir
            existing = db.execute("SELECT * FROM versions WHERE source_id=? AND version_id=?", (sid, version_id)).fetchone()
            if final_dir.exists() or existing:
                verify_version(final_dir, result, asset_manifest)
            metadata = {
                "schema_version": 1, "source_id": sid, "version_id": version_id,
                "original_url": capture["url"], "captured_at": timestamp,
                "processor": "knowledge-capture/0.1.0", "ai_processed": False,
                **{key: value for key, value in result.items() if key not in {"markdown", "original_url"}},
            }
            if not existing:
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".version-", dir=final_dir.parent) as temp:
                    stage = Path(temp) / "version"
                    (stage / "assets").mkdir(parents=True)
                    for relative, _ in asset_manifest:
                        shutil.copy2(assets_dir / Path(relative).name, stage / relative)
                    copy_raw_evidence(result, assets_dir.parent, stage)
                    (stage / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    # JSON values are valid YAML scalars, avoiding frontmatter injection.
                    front = {key: metadata.get(key) for key in (
                        "source_id", "version_id", "title", "original_url", "final_url", "author",
                        "published_at", "captured_at", "status", "processor", "ai_processed"
                    )}
                    header = "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items()) + "\n---\n\n"
                    (stage / "content.md").write_text(header + result["markdown"].strip() + "\n", encoding="utf-8")
                    if final_dir.exists():
                        verify_version(final_dir, result, asset_manifest)
                    else:
                        stage.rename(final_dir)
                db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?)",
                           (sid, version_id, str(relative_dir), result["status"], timestamp))
            db.execute("""INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                latest_version=excluded.latest_version, updated_at=excluded.updated_at""",
                       (sid, capture["url"], result["title"], version_id, timestamp, timestamp))
            db.execute("UPDATE captures SET source_id=?, version_id=?, status=?, finished_at=? WHERE id=?",
                       (sid, version_id, result["status"], timestamp, capture_id))
        from .vault_export import sync_after_commit
        return {"capture_id": capture_id, "source_id": sid, "version_id": version_id,
                "status": result["status"], "unchanged": bool(existing),
                "path": str(final_dir / "content.md"), "warnings": result.get("warnings", []),
                "vault_sync": sync_after_commit(self)}

    def list_sources(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM sources ORDER BY updated_at DESC")]

    def captures(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM captures ORDER BY created_at DESC")]

    def read(self, sid: str, version: str | None = None) -> dict:
        with closing(self._connect()) as db:
            source = db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            if source is None:
                raise ValueError("未找到该来源")
            row = db.execute("SELECT * FROM versions WHERE source_id=? AND version_id=?",
                             (sid, version or source["latest_version"])).fetchone()
            if row is None:
                raise ValueError("未找到该版本")
        directory = self.root / row["path"]
        return {"metadata": json.loads((directory / "metadata.json").read_text(encoding="utf-8")),
                "markdown": (directory / "content.md").read_text(encoding="utf-8"), "path": str(directory)}

    def source_knowledge_state(self, sid, version=None):
        """Read-only authority gate; historical source bytes always stay readable.

        An active labels-only overlay can still use unchanged original evidence.
        Authored revisions are not silently substituted into source citations.
        """
        state={'knowledge_status':'unknown','current_knowledge_version':None,
               'has_overlay':False,'source_eligible':False,'reason':'source_missing'}
        try:
            with closing(self._connect()) as db:
                source=db.execute('SELECT latest_version FROM sources WHERE id=?',(sid,)).fetchone()
                if source is None:
                    return state
                state.update(knowledge_status='active',current_knowledge_version=source['latest_version'])
                present=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_records'").fetchone()
                overlay=db.execute('SELECT latest_version,status FROM knowledge_records WHERE id=?',(sid,)).fetchone() if present else None
            if version is not None and version!=source['latest_version']:
                state['reason']='historical_source'
            else:
                state.update(source_eligible=True,reason=None)
            if overlay is None:
                return state
            state.update(has_overlay=True,knowledge_status=overlay['status'],current_knowledge_version=overlay['latest_version'])
            if overlay['status']=='expired':
                state.update(source_eligible=False,reason='knowledge_expired')
                return state
            from .knowledge_records import KnowledgeRecords
            maintained=KnowledgeRecords(self,initialize=False).read(sid)
            original=self.read(sid)
            same=(maintained['source_version']==source['latest_version']
                  and maintained['markdown']==original['markdown'].split('---\n\n',1)[-1]
                  and maintained['metadata']['title']==original['metadata']['title'])
            if not same:
                state.update(source_eligible=False,reason='knowledge_revised')
            return state
        except (OSError,ValueError,KeyError,TypeError):
            state.update(source_eligible=False,reason='knowledge_integrity_unavailable')
            return state

    def search(self, query: str, limit: int = 10) -> list[dict]:
        terms = query.strip().casefold().split()
        if not terms:
            raise ValueError("搜索词不能为空")
        matches = []
        for source in self.list_sources():
            if not self.source_knowledge_state(source["id"], source["latest_version"])["source_eligible"]:
                continue
            document = self.read(source["id"])
            body = document["markdown"].split("---\n\n", 1)[-1]
            folded = body.casefold()
            if all(term in folded or term in source["title"].casefold() for term in terms):
                index = next((folded.find(term) for term in terms if term in folded), 0)
                matches.append({"source_id": source["id"], "title": source["title"],
                                "version_id": source["latest_version"], "url": source["url"],
                                "excerpt": body[max(0, index - 60):index + 240]})
        return matches[:max(1, min(limit, 100))]

    def export(self, sid: str, output: str | Path) -> Path:
        document = self.read(sid)
        directory = Path(document["path"])
        raw_evidence_files(document['metadata'], directory)
        output = Path(output).expanduser().resolve()
        if output == directory or directory in output.parents:
            raise ValueError("导出位置不能位于来源版本目录内")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents overwriting an existing user archive.
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(directory.rglob("*")):
                if file.is_file() and not file.is_symlink():
                    archive.write(file, str(Path(sid) / file.relative_to(directory)))
        return output
