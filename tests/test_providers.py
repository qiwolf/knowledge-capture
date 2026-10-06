import base64
import json
from unittest.mock import patch

import pytest

from knowledge_capture.providers import CaptureRouter, Configuration, ConfiguredSearch, ProviderError
from knowledge_capture.store import Store


TEXT = """知识采集需要保存完整来源，并且区分用户主动添加的材料与系统推荐的内容。收集到链接之后，必须先检查网页是否真的包含正文，再保留有效的信息、表格和图片。任何处理失败都应留下清楚的原因，以便用户补充资料。
对于设备漏洞的分析，还需要知道设备正在运行的版本，以及这条版本记录的更新时间。旧资料不能自动当作当前事实，推断也不应该覆盖原始证据。这样的知识库才能同时支持人工阅读和机器检索。"""


def config(response, capability="reader", transport="mcp"):
    calls = []
    class FakeConnector:
        def __init__(self, spec):
            self.spec = spec
        def call(self, payload):
            calls.append(payload)
            return response
    data = {"services": {"custom": {"transport": transport, "endpoint": "https://provider.example/mcp", "mcp_tool": "read"}},
            "capabilities": {capability: {"service": "custom", "input_fields": {"url": "request.target", "query": "q", "limit": "count"},
                                           "output_fields": {"markdown": "data.markdown", "title": "data.title", "images": "data.images"}}},
            "routing": {"capture": [{"hosts": ["mp.weixin.qq.com"], "reader": "reader"}]}}
    return Configuration(data, connector_factory=FakeConnector), calls


def test_custom_reader_maps_input_and_preserves_provider_provenance(tmp_path):
    configuration, calls = config({"data": {"markdown": TEXT, "title": "来自用户配置的工具"}})
    store = Store(tmp_path)
    with patch("knowledge_capture.providers._target"):
        result = store.ingest("https://mp.weixin.qq.com/s/article", capture_fn=CaptureRouter(configuration).capture)
    assert calls == [{"request": {"target": "https://mp.weixin.qq.com/s/article"}}]
    assert store.read(result["source_id"])["metadata"]["acquisition"] == {"service": "custom", "transport": "mcp", "capability": "reader"}


def test_provider_supplied_images_do_not_need_browser_cookies(tmp_path):
    image = b"\x89PNG\r\n\x1a\n" + b"fixture"
    configuration, _ = config({"data": {"markdown": TEXT + "\n![图](https://images.example/diagram.png)", "title": "图文",
                                      "images": [{"url": "https://images.example/diagram.png", "mime": "image/png", "base64": base64.b64encode(image).decode()}]}})
    with patch("knowledge_capture.providers._target"), patch("knowledge_capture.providers._request") as fetch:
        result = CaptureRouter(configuration).capture("https://mp.weixin.qq.com/s/article", tmp_path)
    fetch.assert_not_called()
    assert result["status"] == "complete"
    assert (tmp_path / result["assets"][0]["relative_path"].split("/")[-1]).read_bytes() == image


def test_unmatched_domains_use_builtin_reader(tmp_path):
    configuration, calls = config({})
    with patch("knowledge_capture.providers.capture_url", return_value={"status": "test"}) as capture:
        assert CaptureRouter(configuration).capture("https://example.org/a", tmp_path) == {"status": "test"}
    capture.assert_called_once()
    assert calls == []


def test_provider_error_is_not_a_success_page(tmp_path):
    configuration, _ = config({"success": False, "data": {"markdown": TEXT, "title": "错误"}})
    configuration.data["capabilities"]["reader"]["success_path"] = "success"
    with patch("knowledge_capture.providers._target"), pytest.raises(ProviderError, match="未完成"):
        CaptureRouter(configuration).capture("https://mp.weixin.qq.com/s/article", tmp_path)


def test_invalid_provider_image_is_rejected(tmp_path):
    configuration, _ = config({"data": {"markdown": TEXT, "title": "标题", "images": [{"url": "https://images.example/a", "mime": "image/png", "base64": "?bad"}]}})
    with patch("knowledge_capture.providers._target"), pytest.raises(ProviderError, match="图片数据"):
        CaptureRouter(configuration).capture("https://mp.weixin.qq.com/s/article", tmp_path)


def test_search_maps_api_or_mcp_results_and_deduplicates():
    configuration, calls = config({"web": {"items": [{"name": "资料一", "link": "https://example.org/a#one"}, {"name": "资料一", "link": "https://example.org/a#two"}]}}, "search")
    configuration.data["capabilities"]["search"]["output_fields"] = {"results": "web.items", "title": "name", "url": "link"}
    results = ConfiguredSearch(configuration).search("知识整理", 5)
    assert len(results) == 1 and results[0]["url"] == "https://example.org/a"
    assert calls == [{"q": "知识整理", "count": 5}]


def test_broken_search_mapping_is_not_no_results():
    configuration, _ = config({"error": "denied"}, "search")
    with pytest.raises(ProviderError, match="不能视为无结果"):
        ConfiguredSearch(configuration).search("主题")


def test_config_loads_only_explicit_path(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"services": {}, "capabilities": {}}))
    assert Configuration.load(path).data == {"services": {}, "capabilities": {}}
    with pytest.raises(ProviderError):
        Configuration.load(tmp_path / "not-present.json")


def video_config(response, transport="mcp"):
    configuration, calls = config(response, transport=transport)
    spec = configuration.data["capabilities"]["reader"]
    spec["kind"] = "video"
    spec["output_fields"] = {key: "data." + key for key in (
        "title", "status", "segments", "frames", "duration", "transcript_kind")}
    return configuration, calls


def video_result(**changes):
    return {"data": {"title": "设备版本核实", "status": "complete", "duration": 5,
                     "transcript_kind": "asr", "segments": [{"start": 0.25, "end": 2.5, "text": "请先核实版本。"}],
                     "frames": [], **changes}}


@pytest.mark.parametrize("transport", ["mcp", "http_json"])
def test_short_timestamped_video_is_archived_with_evidence(tmp_path, transport):
    configuration, calls = video_config(video_result(), transport)
    store = Store(tmp_path)
    with patch("knowledge_capture.providers._target"):
        result = store.ingest("https://mp.weixin.qq.com/video/example", capture_fn=CaptureRouter(configuration).capture)
    saved = store.read(result["source_id"])
    content = saved["markdown"]
    assert "00:00:00.250–00:00:02.500" in content
    assert "请先核实版本。" in content
    assert "未独立复核音视频" in content
    assert "自动语音转写" in content
    assert saved["metadata"]["acquisition"]["kind"] == "video"
    assert result["status"] == "complete"
    assert calls == [{"request": {"target": "https://mp.weixin.qq.com/video/example"}}]


def test_video_frame_download_and_timestamp(tmp_path):
    image = b"\x89PNG\r\n\x1a\n" + b"frame-fixture"
    frames = [{"timestamp": 1.5, "url": "https://frames.example/one.png", "caption": "服务返回的设备设置画面",
               "base64": base64.b64encode(image).decode(), "mime": "image/png"}]
    configuration, _ = video_config(video_result(frames=frames))
    with patch("knowledge_capture.providers._target"), patch("knowledge_capture.providers._request") as fetch:
        result = CaptureRouter(configuration).capture("https://mp.weixin.qq.com/video/a", tmp_path)
    fetch.assert_not_called()
    assert "### 00:00:01.500" in result["markdown"]
    assert "assets/" in result["markdown"]
    assert result["assets"][0]["status"] == "complete"
    assert (tmp_path / result["assets"][0]["relative_path"].split("/")[-1]).read_bytes() == image


def test_missing_video_frame_makes_partial(tmp_path):
    configuration, _ = video_config(video_result(frames=[{"timestamp": 1, "url": "https://frames.example/missing.png"}]))
    with patch("knowledge_capture.providers._target"), patch("knowledge_capture.providers._request", side_effect=ProviderError("http_error", "图片读取失败")):
        result = CaptureRouter(configuration).capture("https://mp.weixin.qq.com/video/a", tmp_path)
    assert result["status"] == "partial"
    assert result["assets"][0]["status"] == "failed"
    assert "missing.png" in result["markdown"]


@pytest.mark.parametrize("changes", [
    {"status": "queued"}, {"status": "running"}, {"segments": []},
    {"segments": [{"text": "没有时间戳"}]},
    {"segments": [{"start": 2, "end": 1, "text": "倒序"}]},
    {"segments": [{"start": -1, "end": 1, "text": "负数"}]},
    {"segments": [{"start": 0, "end": float("inf"), "text": "无穷值"}]},
    {"segments": [{"start": 0, "end": 1, "text": ""}]},
    {"segments": [{"start": 0, "end": 6, "text": "超出时长"}]},
    {"frames": [{"timestamp": 6, "url": "https://frames.example/a.png"}]},
])
def test_invalid_or_pending_video_is_not_saved(tmp_path, changes):
    configuration, _ = video_config(video_result(**changes))
    store = Store(tmp_path)
    with patch("knowledge_capture.providers._target"), pytest.raises(ProviderError):
        store.ingest("https://mp.weixin.qq.com/video/a", capture_fn=CaptureRouter(configuration).capture)
    assert store.list_sources() == []
    assert store.captures()[0]["status"] == "failed"


def test_partial_video_and_nested_segment_mapping(tmp_path):
    configuration, _ = video_config(video_result(status="partial", segments=[{"time": {"from": 0, "to": 1}, "content": "已有的字幕"}]))
    configuration.data["capabilities"]["reader"]["output_fields"].update(
        segment_start="time.from", segment_end="time.to", segment_text="content")
    with patch("knowledge_capture.providers._target"):
        result = CaptureRouter(configuration).capture("https://mp.weixin.qq.com/video/a", tmp_path)
    assert result["status"] == "partial"
    assert "已有的字幕" in result["markdown"]
    assert any("部分完成" in warning for warning in result["warnings"])


def test_transcript_markdown_is_evidence_not_an_image_request(tmp_path):
    configuration, _ = video_config(video_result(segments=[{"start": 0, "end": 1, "text": "![外部图](http://127.0.0.1/private) <script>evil</script>"}]))
    with patch("knowledge_capture.providers._target"), patch("knowledge_capture.providers._request") as fetch:
        result = CaptureRouter(configuration).capture("https://mp.weixin.qq.com/video/a", tmp_path)
    fetch.assert_not_called()
    assert "<script>" not in result["markdown"]
    assert result["assets"] == []


@pytest.mark.parametrize('flag', [True, False])
def test_explicit_truncation_from_mcp_is_preserved(tmp_path, flag):
    configuration, _ = config({'truncated': flag, 'data': {'markdown': TEXT, 'title': '读取结果'}})
    configuration.data['capabilities']['reader']['truncated_path'] = 'truncated'
    with patch('knowledge_capture.providers._target'):
        result = Store(tmp_path).ingest('https://mp.weixin.qq.com/s/article', capture_fn=CaptureRouter(configuration).capture)
    assert result['status'] == ('partial' if flag else 'complete')
    assert any('截断' in warning for warning in result['warnings']) == flag


def test_missing_or_invalid_truncation_cannot_claim_complete(tmp_path):
    configuration, _ = config({'data': {'markdown': TEXT, 'title': '读取结果'}})
    configuration.data['capabilities']['reader']['truncated_path'] = 'truncated'
    with patch('knowledge_capture.providers._target'), pytest.raises(ProviderError, match='截断标记'):
        CaptureRouter(configuration).capture('https://mp.weixin.qq.com/s/article', tmp_path)
