"""Explicit endpoint connections. HTTP opt-in sends data/tokens without encryption.

MCP requires official mcp>=1.26,<2. It uses Streamable HTTP, not legacy SSE.
Explicit discovery only lists tools; callback execution and retries are disabled.
"""
import asyncio
import ipaddress
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .llm import _strict_json

LIMIT = 2 * 1024 * 1024


class ConnectorError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def load_services(path):
    try:
        with Path(path).open("rb") as source:
            raw = source.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError()
        document = _strict_json(raw.decode("utf-8"))
        services = document["services"]
        if not isinstance(services, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in services.items()):
            raise ValueError()
        for value in services.values():
            Connector(value)
        return services
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise ConnectorError("configuration_invalid", "无法读取有效的连接器配置文件。") from None


class Connector:
    def __init__(self, config, credential_values=None):
        try:
            if not isinstance(config, dict) or set(config) - {"transport", "endpoint", "token_env", "headers_env", "mcp_tool", "timeout_seconds", "allow_insecure_http", "method", "response_format"}:
                raise ValueError()
            if credential_values is not None and (not isinstance(credential_values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in credential_values.items())):
                raise ValueError()
            self._credential_values = dict(credential_values or {})
            self.transport = config["transport"]
            self.endpoint = config["endpoint"]
            self.timeout = config.get("timeout_seconds", 60)
            self.token_env = config.get("token_env")
            self.headers_env = config.get("headers_env", {})
            self.tool = config.get("mcp_tool")
            self.method = config.get("method", "POST")
            self.response_format = config.get("response_format", "json")
            if (self.method not in {"GET", "POST"} or self.response_format not in {"json", "text", "auto"}
                    or (self.transport == "mcp" and "method" in config)):
                raise ValueError()
            if self.transport not in {"http_json", "mcp"} or not isinstance(self.endpoint, str):
                raise ValueError()
            parts = urlsplit(self.endpoint)
            if (not parts.hostname or parts.username is not None or parts.password is not None
                    or "?" in self.endpoint or "#" in self.endpoint
                    or any(ord(c) <= 32 or ord(c) == 127 for c in self.endpoint)):
                raise ValueError()
            parts.port
            if parts.scheme != "https":
                if parts.scheme != "http" or config.get("allow_insecure_http") is not True:
                    raise ValueError()
                if parts.hostname != "localhost":
                    address = ipaddress.ip_address(parts.hostname)
                    private = any(address in ipaddress.ip_network(network) for network in (
                        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
                        if address.version == ipaddress.ip_network(network).version)
                    if not private:
                        raise ValueError()
            if type(self.timeout) not in (int, float) or not 1 <= self.timeout <= 600:
                raise ValueError()
            if self.token_env is not None and (not isinstance(self.token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.token_env)):
                raise ValueError()
            if not isinstance(self.headers_env, dict):
                raise ValueError()
            for header, variable in self.headers_env.items():
                if (not isinstance(header, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", header)
                        or header.lower() in {"host", "content-length", "transfer-encoding", "accept-encoding"}
                        or not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable)):
                    raise ValueError()
            if self.token_env and any(header.lower() == "authorization" for header in self.headers_env):
                raise ValueError()
            if self.transport == "mcp" and (not isinstance(self.tool, str) or not self.tool.strip()):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ConnectorError("configuration_invalid", "连接器配置无效：请使用 HTTPS、环境变量密钥引用和有效工具名；私网明文 HTTP 必须明确启用。") from None

    @classmethod
    def from_file(cls, path, service):
        services = load_services(path)
        if service not in services:
            raise ConnectorError("service_missing", "配置中未找到指定连接器。")
        return cls(services[service])

    def list_tools(self):
        """Read MCP metadata without invoking any tool."""
        if self.transport != "mcp":
            raise ConnectorError("configuration_invalid", "仅 MCP 服务支持工具发现。")
        return self.call({}, _discover=True)

    def call(self, payload, *, _discover=False):
        try:
            if not isinstance(payload, dict):
                raise ValueError()
            json.dumps(payload, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ConnectorError("invalid_input", "连接器参数必须是有效 JSON 对象。") from None
        headers = {}
        for header, variable in self.headers_env.items():
            value = os.environ.get(variable, self._credential_values.get(variable, ""))
            if not value.strip():
                raise ConnectorError("credential_missing", "连接器指定的请求头环境变量尚未设置。")
            headers[header] = value
        if self.token_env:
            token = os.environ.get(self.token_env, self._credential_values.get(self.token_env, ""))
            if not token.strip():
                raise ConnectorError("credential_missing", "连接器指定的密钥环境变量尚未设置。")
            headers["Authorization"] = "Bearer " + token
        if self.transport == "http_json":
            return self._http(payload, headers)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ConnectorError("async_context", "同步连接器不能在已有异步事件循环中调用，请从工作线程调用。")
        try:
            return asyncio.run(self._mcp(payload, headers, discover=True) if _discover else self._mcp(payload, headers))
        except ConnectorError:
            raise
        except (TimeoutError, requests.exceptions.Timeout):
            raise ConnectorError("timeout", "连接器调用超时，未自动重试。") from None
        except ImportError:
            raise ConnectorError("dependency_missing", "MCP 连接器需要安装官方 mcp Python SDK。") from None
        except Exception as exc:
            # AnyIO task groups may wrap a transport guard error in ExceptionGroup.
            pending = [exc]
            while pending:
                current = pending.pop()
                if isinstance(current, ConnectorError):
                    raise ConnectorError(current.code, str(current)) from None
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
            raise ConnectorError("mcp_failed", "MCP 调用未完成，未自动重试；请核实服务状态。") from None

    def _http(self, payload, headers):
        try:
            with requests.Session() as session:
                session.trust_env = False
                request = session.get if self.method == "GET" else session.post
                arguments = {"params": payload} if self.method == "GET" else {"json": payload}
                with request(self.endpoint, **arguments, headers=headers, stream=True,
                             allow_redirects=False, timeout=(10, self.timeout)) as response:
                    _status(response.status_code)
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > LIMIT:
                        raise ConnectorError("response_too_large", "连接器响应超过大小限制。")
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > LIMIT:
                            raise ConnectorError("response_too_large", "连接器响应超过大小限制。")
            if self.response_format == "text":
                return _text(bytes(data))
            if self.response_format == "auto":
                try:
                    return _parse(bytes(data))
                except ConnectorError as exc:
                    if exc.code != "invalid_response":
                        raise
                    return _text(bytes(data))
            return _parse(bytes(data))
        except ConnectorError:
            raise
        except requests.exceptions.Timeout:
            raise ConnectorError("timeout", "连接器调用超时，未自动重试。") from None
        except Exception:
            raise ConnectorError("connection_failed", "连接器请求失败，未自动重试。") from None

    async def _mcp(self, payload, headers, discover=False):
        import httpx
        from mcp import ClientSession, types
        from mcp.client.streamable_http import streamable_http_client

        class BoundedStream(httpx.AsyncByteStream):
            def __init__(self, stream):
                self.stream = stream

            async def __aiter__(self):
                size = 0
                async for chunk in self.stream:
                    size += len(chunk)
                    if size > LIMIT:
                        raise ConnectorError("response_too_large", "连接器响应超过大小限制。")
                    yield chunk

            async def aclose(self):
                await self.stream.aclose()

        class GuardedTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

            async def handle_async_request(self, request):
                response = await self.inner.handle_async_request(request)
                try:
                    # 202 notifications, 405 optional GET, 404 session cleanup are legitimate.
                    if 300 <= response.status_code < 400:
                        _status(response.status_code)
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ConnectorError("encoding_unsupported", "MCP 服务忽略了未压缩响应要求。")
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > LIMIT:
                        raise ConnectorError("response_too_large", "连接器响应超过大小限制。")
                    response.stream = BoundedStream(response.stream)
                    return response
                except Exception:
                    await response.aclose()
                    raise

            async def aclose(self):
                await self.inner.aclose()

        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(headers={**headers, "Accept-Encoding": "identity"},
                                         timeout=httpx.Timeout(self.timeout, connect=10), trust_env=False,
                                         follow_redirects=False, transport=GuardedTransport()) as client:
                async with streamable_http_client(self.endpoint, http_client=client) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                        await session.initialize()
                        if discover:
                            found, cursor = [], None
                            for _ in range(8):
                                page = await session.list_tools(cursor=cursor)
                                found.extend(tool.model_dump(by_alias=True, exclude_none=True) for tool in page.tools)
                                if len(found) > 200 or len(json.dumps(found).encode()) > LIMIT:
                                    raise ConnectorError("response_too_large", "工具列表超过发现限制。")
                                cursor = page.nextCursor
                                if not cursor:
                                    return found
                            raise ConnectorError("response_too_large", "工具列表分页超过发现限制。")
                        # Public protocol request avoids call_tool's implicit tools/list.
                        # The capability layer validates the application's result schema.
                        result = await session.send_request(
                            types.ClientRequest(types.CallToolRequest(
                                params=types.CallToolRequestParams(name=self.tool, arguments=payload))),
                            types.CallToolResult,
                        )
        if result.isError:
            raise ConnectorError("tool_error", "MCP 工具返回执行失败。")
        if self.response_format == "auto":
            # Preserve every returned block, including non-text metadata, for archive
            # and bounded content normalization. Never choose one representation here.
            envelope = {"content": [item.model_dump(by_alias=True, exclude_none=True) for item in result.content]}
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                envelope["structuredContent"] = structured
            try:
                return _parse(json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (ValueError, TypeError):
                raise ConnectorError("invalid_response", "MCP 返回内容无法完整归档。") from None
        text = [item.text for item in result.content if getattr(item, "type", None) == "text"]
        if self.response_format == "text":
            if not text:
                raise ConnectorError("invalid_response", "MCP 工具未返回所需的文本内容。")
            return _text("\n".join(text).encode("utf-8"))
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            try:
                return _parse(json.dumps(structured, allow_nan=False).encode())
            except (ValueError, TypeError):
                raise ConnectorError("invalid_response", "MCP 工具返回的结构化数据无效。") from None
        if len(text) != 1:
            raise ConnectorError("invalid_response", "MCP 工具必须返回结构化数据或单个 JSON 文本。")
        return _parse(text[0].encode("utf-8"))


def _status(status):
    if 300 <= status < 400:
        raise ConnectorError("redirect_rejected", "连接器返回跳转，已停止请求以保护凭据。")
    if status in {401, 403}:
        raise ConnectorError("authentication_failed", "连接器认证失败或没有访问权限。")
    if status == 429:
        raise ConnectorError("rate_limited", "连接器请求受限，请稍后重试。")
    if status == 202:
        raise ConnectorError("not_ready", "服务仅接受了请求，尚未返回完整内容。")
    if status != 200:
        raise ConnectorError("http_error", "连接器服务返回错误。")


def _parse(raw):
    if len(raw) > LIMIT:
        raise ConnectorError("response_too_large", "连接器响应超过大小限制。")
    try:
        value = _strict_json(raw.decode("utf-8"))
        if not isinstance(value, (dict, list)):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectorError("invalid_response", "连接器必须返回有效 JSON 对象或数组。") from None


def _text(raw):
    if len(raw) > LIMIT:
        raise ConnectorError("response_too_large", "连接器响应超过大小限制。")
    try:
        return {"text": raw.decode("utf-8")}
    except UnicodeError:
        raise ConnectorError("invalid_response", "连接器文本响应必须是有效 UTF-8。") from None
