import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from knowledge_capture.capture import CaptureError
from knowledge_capture.store import Store, canonical_url


def capture_result(text="正文：这是用于测试版本更新的中文知识。", image=False):
    def capture(url, directory):
        assets = []
        if image:
            data = b"test-image"
            (directory / "diagram.png").write_bytes(data)
            assets = [{"relative_path": "assets/diagram.png", "sha256": hashlib.sha256(data).hexdigest(),
                       "status": "complete", "original_url": "https://example.org/diagram.png"}]
        return {"title": "中文资料", "markdown": text + ("\n![示意图](assets/diagram.png)" if image else ""),
                "original_url": url, "final_url": url, "author": None, "published_at": None,
                "assets": assets, "warnings": [], "status": "complete"}
    return capture


def test_duplicate_capture_keeps_intent_without_duplicate_version(tmp_path):
    store = Store(tmp_path)
    a = store.ingest("https://example.org/article#one", "研究一", capture_fn=capture_result())
    b = store.ingest("https://example.org/article#two", "研究二", capture_fn=capture_result())
    assert a["source_id"] == b["source_id"]
    assert a["version_id"] == b["version_id"]
    assert b["unchanged"] is True
    assert len(store.list_sources()) == 1
    assert {record["note"] for record in store.captures()} == {"研究一", "研究二"}
    assert len(list(tmp_path.glob("sources/*/versions/*/content.md"))) == 1


def test_changed_content_preserves_old_version_and_searches_current(tmp_path):
    store = Store(tmp_path)
    a = store.ingest("https://example.org/article", capture_fn=capture_result("旧版：系统使用密码登录。"))
    b = store.ingest("https://example.org/article", capture_fn=capture_result("新版：系统增加多因素验证。"))
    assert a["version_id"] != b["version_id"]
    assert "密码" in store.read(a["source_id"], a["version_id"])["markdown"]
    assert "多因素" in store.read(a["source_id"])["markdown"]
    assert store.search("多因素")[0]["source_id"] == a["source_id"]
    assert store.search("密码") == []


def test_failed_capture_is_visible_but_not_a_source(tmp_path):
    store = Store(tmp_path)
    def denied(url, directory):
        raise CaptureError("no_content", "需要访问验证")
    with pytest.raises(CaptureError):
        store.ingest("https://example.org/article", capture_fn=denied)
    assert store.list_sources() == []
    assert store.captures()[0]["status"] == "failed"
    assert store.captures()[0]["error_code"] == "no_content"


def test_missing_asset_cannot_be_complete(tmp_path):
    store = Store(tmp_path)
    def missing(url, directory):
        result = capture_result()(url, directory)
        result["assets"] = [{"status": "failed"}]
        return result
    with pytest.raises(ValueError, match="部分完成"):
        store.ingest("https://example.org/article", capture_fn=missing)
    assert not store.list_sources()
    assert store.captures()[0]["status"] == "failed"


def test_partial_capture_preserves_warning_and_discovery_origin(tmp_path):
    store = Store(tmp_path)
    def partial(url, directory):
        result = capture_result()(url, directory)
        result.update(status="partial", assets=[{"status": "failed"}], warnings=["图片下载失败"])
        return result
    result = store.ingest("https://example.org/article", origin="discovery", capture_fn=partial)
    assert result["status"] == "partial"
    assert store.captures()[0]["origin"] == "discovery"
    assert store.read(result["source_id"])["metadata"]["warnings"] == ["图片下载失败"]


def test_export_keeps_relative_images_and_does_not_overwrite(tmp_path):
    store = Store(tmp_path / "data")
    result = store.ingest("https://example.org/article", capture_fn=capture_result(image=True))
    output = store.export(result["source_id"], tmp_path / "export.zip")
    with zipfile.ZipFile(output) as archive:
        sid = result["source_id"]
        assert f"{sid}/assets/diagram.png" in archive.namelist()
        assert "assets/diagram.png" in archive.read(f"{sid}/content.md").decode()
        assert json.loads(archive.read(f"{sid}/metadata.json"))["ai_processed"] is False
    with pytest.raises(FileExistsError):
        store.export(result["source_id"], output)


def test_asset_path_traversal_is_rejected(tmp_path):
    store = Store(tmp_path)
    def unsafe(url, directory):
        result = capture_result(image=True)(url, directory)
        result["assets"][0]["relative_path"] = "assets/../../private"
        return result
    with pytest.raises(ValueError, match="图片路径"):
        store.ingest("https://example.org/article", capture_fn=unsafe)


def test_corrupted_existing_image_is_not_reused_as_success(tmp_path):
    store = Store(tmp_path)
    result = store.ingest("https://example.org/article", capture_fn=capture_result(image=True))
    image = Path(result["path"]).parent / "assets/diagram.png"
    image.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="完整性"):
        store.ingest("https://example.org/article", capture_fn=capture_result(image=True))
    assert image.read_bytes() == b"corrupted"  # Preserve evidence; do not silently repair.
    assert store.captures()[0]["status"] == "failed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.org/page", "not-a-url"])
def test_invalid_links_are_not_registered(tmp_path, url):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        store.ingest(url, capture_fn=capture_result())
    assert store.captures() == []
