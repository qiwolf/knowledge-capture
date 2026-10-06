"""User-configured API/MCP capabilities, independent of collection entrypoints."""
from __future__ import annotations

import base64
import copy
import json
import math
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .capture import CaptureError, _request, _target, capture_url, extract_html, extract_markdown
from .store import canonical_url


class ProviderError(CaptureError):
    pass


def _normalize_engine(response, purpose, store, client, source_url=None):
    from .engine_content import EngineContentNormalizer
    from .llm import CloudClient
    if store is None:
        raise ProviderError('configuration_missing', '自动识别引擎需要知识库目录以保留原始响应')
    return EngineContentNormalizer(client or CloudClient.for_store(store)).normalize(
        response, purpose=purpose, source_url=source_url, archive_dir=store.root / 'engine_responses')


def get_path(data, path: str, default=None):
    if path == "":
        return data
    if not isinstance(path, str) or len(path.split(".")) > 20:
        raise ProviderError("mapping_invalid", "服务字段映射不正确")
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def set_path(data: dict, path: str, value):
    if not isinstance(path, str) or not path or len(path.split(".")) > 20:
        raise ProviderError("mapping_invalid", "服务输入字段映射不正确")
    current = data
    parts = path.split(".")
    for part in parts[:-1]:
        if not part:
            raise ProviderError("mapping_invalid", "服务输入字段映射不正确")
        current = current.setdefault(part, {})
        if not isinstance(current, dict):
            raise ProviderError("mapping_invalid", "服务输入映射与固定参数冲突")
    current[parts[-1]] = value


class Configuration:
    def __init__(self, config: dict, connector_factory=None):
        if not isinstance(config, dict) or not isinstance(config.get("services"), dict) or not isinstance(config.get("capabilities"), dict):
            raise ProviderError("configuration_invalid", "配置必须包含 services 和 capabilities 对象")
        self.data = copy.deepcopy(config)
        self.connector_factory = connector_factory

    @classmethod
    def load(cls, path: str | Path):
        from .llm import _strict_json
        try:
            data = Path(path).read_bytes()
            if len(data) > 256 * 1024:
                raise ValueError()
            return cls(_strict_json(data.decode("utf-8")))
        except ProviderError:
            raise
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("configuration_invalid", "无法读取有效的连接器配置文件") from None

    def capability(self, name: str) -> dict:
        capability = self.data["capabilities"].get(name)
        if not isinstance(capability, dict) or capability.get("service") not in self.data["services"]:
            raise ProviderError("capability_missing", "未配置所需的服务能力")
        if not isinstance(capability.get("output_fields", {}), dict):
            raise ProviderError("mapping_invalid", "服务输出映射不正确")
        return capability

    def call(self, name: str, arguments: dict):
        from .connectors import Connector, ConnectorError
        capability = self.capability(name)
        payload = copy.deepcopy(capability.get("constants", {}))
        mappings = capability.get("input_fields", {})
        if not isinstance(payload, dict) or not isinstance(mappings, dict):
            raise ProviderError("mapping_invalid", "服务输入映射不正确")
        for key, value in arguments.items():
            target = mappings.get(key, key)
            if target is None and capability.get('auto_content') is True:
                continue
            set_path(payload, target, value)
        try:
            result = (self.connector_factory or Connector)(self.data["services"][capability["service"]]).call(payload)
        except ConnectorError as exc:
            raise ProviderError(exc.code, str(exc)) from None
        if "success_path" in capability and get_path(result, capability["success_path"]) != capability.get("success_value", True):
            raise ProviderError("provider_failed", "读取服务报告处理未完成")
        return result


class ConfiguredSearch:
    def __init__(self, configuration: Configuration, capability: str = "search", *, store=None, client=None):
        self.configuration = configuration
        self.name = capability
        self.store, self.client = store, client

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 600 or type(limit) is not int or not 1 <= limit <= 20:
            raise ProviderError("invalid_query", "搜索词不能为空或过长，结果数量须为1至20")
        spec = self.configuration.capability(self.name)
        response = self.configuration.call(self.name, {"query": query, "limit": limit})
        if spec.get('auto_content') is True:
            result = _normalize_engine(response, 'search', self.store, self.client)
            self.last_provenance = result['provenance']
            seen, selected = set(), []
            for item in result['results']:
                if item['url'] not in seen:
                    seen.add(item['url'])
                    selected.append(item)
            return selected[:limit]
        fields = spec.get("output_fields", {})
        items = get_path(response, fields.get("results", "results"))
        if not isinstance(items, list):
            raise ProviderError("invalid_results", "搜索服务没有返回约定的结果数组，不能视为无结果")
        results, seen = [], set()
        for item in items:
            title = get_path(item, fields.get("title", "title"))
            url = get_path(item, fields.get("url", "url"))
            description = get_path(item, fields.get("description", "description"), "")
            if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not isinstance(description, (str, type(None))):
                raise ProviderError("invalid_results", "搜索条目缺少标题或有效链接")
            try:
                url = canonical_url(url)
            except ValueError:
                continue
            if url not in seen:
                seen.add(url)
                results.append({"title": title, "url": url, "description": description or ""})
        return results[:limit]


class CaptureRouter:
    def __init__(self, configuration: Configuration, *, store=None, client=None):
        self.configuration = configuration
        self.store, self.client = store, client

    def _reader(self, url: str) -> str:
        routing = self.configuration.data.get("routing", {})
        if not isinstance(routing, dict) or not isinstance(routing.get("capture", []), list):
            raise ProviderError("routing_invalid", "采集路由配置不正确")
        host = (urlsplit(url).hostname or "").lower()
        for rule in routing.get("capture", []):
            if not isinstance(rule, dict) or not isinstance(rule.get("hosts"), list):
                raise ProviderError("routing_invalid", "采集路由配置不正确")
            for pattern in rule["hosts"]:
                if not isinstance(pattern, str):
                    raise ProviderError("routing_invalid", "采集路由主机名不正确")
                pattern = pattern.lower()
                if host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:])):
                    return rule.get("reader", "builtin")
        return routing.get("default_reader", "builtin")

    def capture(self, url: str, asset_dir: Path) -> dict:
        if getattr(self.configuration, 'reader_error', None):
            raise ProviderError('engine_not_ready', self.configuration.reader_error)
        url = canonical_url(url)
        reader = self._reader(url)
        if reader == "builtin":
            return capture_url(url, asset_dir)
        # Provider-side DNS and network restrictions remain the provider's responsibility.
        _target(url)
        spec = self.configuration.capability(reader)
        response = self.configuration.call(reader, {"url": url})
        if spec.get('auto_content') is True:
            normalized = _normalize_engine(response, 'capture', self.store, self.client, source_url=url)
            final_url = canonical_url(normalized.get('final_url', url))
            _target(final_url)
            # The model selects source spans; it never supplies executable Markdown.
            markdown = re.sub(r'([\\`*_{}\[\]()#+.!|<>-])', r'\\\1', normalized['text'])
            for image in normalized.get('images', []):
                markdown += '\n\n![原文图片](<' + image['url'] + '>)'
            result = extract_markdown(markdown, final_url, asset_dir, title=normalized['title'], original_url=url)
            result['warnings'].extend(normalized.get('warnings', []))
            if result['warnings']:
                result['status'] = 'partial'
            service = self.configuration.data['services'][spec['service']]
            result['acquisition'] = {'service': spec['service'], 'transport': service['transport'],
                                     'capability': reader, 'kind': normalized['kind'],
                                     'normalization': normalized['provenance']}
            shutil.copytree(Path(normalized['provenance']['raw_path']).parent, Path(asset_dir).parent / 'raw')
            return result
        truncated = False
        if "truncated_path" in spec:
            truncated = get_path(response, spec["truncated_path"])
            if type(truncated) is not bool:
                raise ProviderError("invalid_result", "服务未返回约定的正文截断标记")
        fields = spec.get("output_fields", {})
        if not isinstance(fields, dict):
            raise ProviderError("mapping_invalid", "读取结果字段映射不正确")
        if spec.get("kind") == "video":
            result = self._video(response, fields, url, asset_dir)
            service = self.configuration.data["services"][spec["service"]]
            result["acquisition"] = {"service": spec["service"], "transport": service["transport"],
                                     "capability": reader, "kind": "video"}
            if truncated:
                result["status"] = "partial"
                result["warnings"].append("读取服务报告内容已截断，此版本不包含完整材料。")
            return result
        values = {key: get_path(response, fields.get(key, key)) for key in ("html", "markdown", "title", "final_url", "images")}
        final_url = canonical_url(values["final_url"] or url)
        _target(final_url)
        images = values["images"] or []
        if not isinstance(images, list):
            raise ProviderError("invalid_images", "读取服务图片字段必须为数组")
        supplied = {}
        for item in images:
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("url", "base64", "mime")):
                raise ProviderError("invalid_images", "读取服务图片需包含 url、base64 和 mime")
            try:
                binary = base64.b64decode(item["base64"], validate=True)
                if len(binary) > 12 * 1024 * 1024:
                    raise ValueError()
                supplied[canonical_url(item["url"])] = (binary, item["mime"], item["url"])
            except ValueError:
                raise ProviderError("invalid_images", "读取服务图片数据无效或过大") from None

        def image_loader(image_url, limit):
            if image_url in supplied:
                return supplied[image_url]
            return _request(image_url, limit)

        if isinstance(values["html"], str) and values["html"].strip():
            result = extract_html(values["html"].encode("utf-8"), final_url, asset_dir, original_url=url, image_loader=image_loader)
        elif isinstance(values["markdown"], str) and values["markdown"].strip():
            title = values["title"]
            if not isinstance(title, str) or not title.strip():
                heading = re.search(r"^#\s+(.+)$", values["markdown"], re.M)
                title = heading.group(1).strip() if heading else urlsplit(final_url).hostname
            result = extract_markdown(values["markdown"], final_url, asset_dir, title=title, original_url=url, image_loader=image_loader)
        else:
            raise ProviderError("no_content", "读取服务未返回约定的 HTML 或 Markdown 正文")
        service = self.configuration.data["services"][spec["service"]]
        result["acquisition"] = {"service": spec["service"], "transport": service["transport"], "capability": reader}
        if truncated:
            result["status"] = "partial"
            result["warnings"].append("读取服务报告正文已截断，此版本不包含完整材料。")
        return result

    def _video(self, response, fields, url, asset_dir):
        """Archive configured transcriber evidence; never infer unreturned speech or frames.

        Root output fields: status, title, final_url, segments, frames, duration,
        transcript_kind. Item fields: segment_start/end/text and frame_timestamp/
        url/caption/base64/mime, each mapped relative to its segment/frame object.
        All times are numeric seconds. Only completed/partial provider jobs qualify.
        """
        def root(key, default=None):
            return get_path(response, fields.get(key, key), default)

        def item_value(item, prefix, key, default=None):
            return get_path(item, fields.get(prefix + "_" + key, key), default)

        def seconds(value):
            if type(value) not in (int, float) or not 0 <= value <= 1_000_000_000 or not math.isfinite(value):
                raise ProviderError("invalid_transcript", "视频时间戳必须是有限的非负秒数")
            return value

        def stamp(value):
            milliseconds = round(value * 1000)
            hours, rest = divmod(milliseconds, 3600000)
            minutes, rest = divmod(rest, 60000)
            sec, ms = divmod(rest, 1000)
            return f"{hours:02}:{minutes:02}:{sec:02}.{ms:03}"

        def escaped(value):
            # Transcripts and captions are evidence text, never executable Markdown/images.
            value = " ".join(value.splitlines()).strip()
            value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return re.sub(r"([\\`*{}_\[\]()#!|])", r"\\\1", value)

        status = root("status")
        if not isinstance(status, str) or status not in {"complete", "partial"}:
            raise ProviderError("video_not_ready", "视频服务未返回已完成或部分完成状态，未归档为成功")
        title = root("title")
        segments, frames = root("segments"), root("frames", [])
        if (not isinstance(title, str) or not title.strip() or not isinstance(segments, list)
                or not segments or len(segments) > 20000 or not isinstance(frames, list) or len(frames) > 100):
            raise ProviderError("invalid_transcript", "视频结果需包含标题、带时间戳的字幕数组及有效帧数组")
        final_url = canonical_url(root("final_url") or url)
        _target(final_url)
        duration = root("duration")
        if duration is not None:
            duration = seconds(duration)
        kind = root("transcript_kind", "unspecified")
        kinds = {"captions": "来源字幕", "asr": "服务自动语音转写", "manual": "服务提供的人工转写", "unspecified": "服务未说明生成方式"}
        if not isinstance(kind, str) or kind not in kinds:
            raise ProviderError("invalid_transcript", "视频字幕生成方式字段无效")
        lines = [f"# {escaped(title)}", "", "## 视频来源与处理依据", "",
                 f"来源：{escaped(final_url)}", f"字幕依据：{kinds[kind]}。以下内容由用户配置的服务返回，系统未独立复核音视频。",
                 "时间戳：服务提供的秒数，显示精度为毫秒。"]
        if duration is not None:
            lines.append(f"服务报告时长：{stamp(duration)}")
        lines.extend(["", "## 带时间戳字幕", ""])
        previous = -1
        for segment in segments:
            if not isinstance(segment, dict):
                raise ProviderError("invalid_transcript", "视频字幕条目格式无效")
            start, end = (seconds(item_value(segment, "segment", field)) for field in ("start", "end"))
            text = item_value(segment, "segment", "text")
            if (end < start or start < previous or (duration is not None and end > duration)
                    or not isinstance(text, str) or not text.strip()):
                raise ProviderError("invalid_transcript", "视频字幕缺少有效文本或时间范围、顺序不正确")
            previous = start
            lines.append(f"- **[{stamp(start)}–{stamp(end)}]** {escaped(text)}")
        supplied = {}
        if frames:
            lines.extend(["", "## 服务提供的关键帧", ""])
        for frame in frames:
            if not isinstance(frame, dict):
                raise ProviderError("invalid_frames", "视频帧条目格式无效")
            timestamp = seconds(item_value(frame, "frame", "timestamp"))
            if duration is not None and timestamp > duration:
                raise ProviderError("invalid_frames", "视频帧时间戳超出报告的视频时长")
            frame_url = item_value(frame, "frame", "url")
            caption = item_value(frame, "frame", "caption", "")
            if not isinstance(frame_url, str) or not isinstance(caption, str):
                raise ProviderError("invalid_frames", "视频帧缺少有效来源链接或说明")
            frame_url = canonical_url(frame_url)
            # Markdown URL delimiters must not change the generated image structure.
            frame_url = frame_url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
            data, mime = item_value(frame, "frame", "base64"), item_value(frame, "frame", "mime")
            if data is not None:
                try:
                    if not isinstance(data, str) or not isinstance(mime, str):
                        raise ValueError()
                    binary = base64.b64decode(data, validate=True)
                    if len(binary) > 12 * 1024 * 1024:
                        raise ValueError()
                    frame_data = (binary, mime, frame_url)
                    if frame_url in supplied and supplied[frame_url] != frame_data:
                        raise ProviderError("invalid_frames", "相同视频帧链接对应不同图像，无法可靠归档")
                    supplied[frame_url] = frame_data
                except ValueError:
                    raise ProviderError("invalid_frames", "视频帧图像数据无效或过大") from None
            lines.extend([f"### {stamp(timestamp)}", "", escaped(caption) if caption else "服务提供的画面；未额外推断含义。",
                          "", f"![视频帧 {stamp(timestamp)}]({frame_url})", ""])

        def loader(image_url, limit):
            return supplied[image_url] if image_url in supplied else _request(image_url, limit)

        result = extract_markdown("\n".join(lines), final_url, asset_dir, title=title,
                                  original_url=url, image_loader=loader, validate_article=False)
        if status == "partial":
            result["status"] = "partial"
            result["warnings"].append("视频服务报告部分完成，字幕或画面可能不完整。")
        return result
