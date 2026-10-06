"""Optional Brave Web Search adapter; configuration never triggers a request.

Contract: https://api-dashboard.search.brave.com/api-reference/web/search/get
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import requests

from .llm import _strict_json


class SearchError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class SearchClient:
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
    RESPONSE_LIMIT = 2 * 1024 * 1024

    def __init__(self, api_key: str, provider: str = "brave"):
        if provider != "brave":
            raise SearchError("configuration_invalid", "请将搜索服务配置为支持的提供商 brave。")
        if not isinstance(api_key, str) or not api_key.strip():
            raise SearchError("configuration_missing", "请配置搜索服务 API 密钥后再执行检索。")
        self._api_key = api_key.strip()

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("KC_SEARCH_API_KEY", ""), os.environ.get("KC_SEARCH_PROVIDER", ""))

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if (not isinstance(query, str) or not query.strip() or len(query) > 600
                or len(query.split()) > 75 or type(limit) is not int or not 1 <= limit <= 20):
            raise SearchError("invalid_input", "查询不能为空或超过 600 字符、75 个词，结果数量须为 1 至 20。")
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.get(self.ENDPOINT, params={"q": query, "count": limit},
                                 headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
                                 timeout=(10, 30), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise SearchError("redirect_rejected", "搜索服务返回跳转，已停止请求以保护凭据。")
                    if status in {401, 403}:
                        raise SearchError("authentication_failed", "搜索服务认证失败或当前账号没有访问权限。")
                    if status == 429:
                        raise SearchError("rate_limited", "搜索服务额度不足或请求过于频繁，请稍后重试。")
                    if status >= 500:
                        raise SearchError("service_unavailable", "搜索服务暂时不可用，请稍后重试。")
                    if status != 200:
                        raise SearchError("http_error", "搜索服务返回错误，本次检索未完成。")
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > self.RESPONSE_LIMIT:
                        raise SearchError("response_too_large", "搜索响应超过大小限制。")
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > self.RESPONSE_LIMIT:
                            raise SearchError("response_too_large", "搜索响应超过大小限制。")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except SearchError:
            raise
        except requests.exceptions.Timeout:
            raise SearchError("timeout", "搜索服务连接或读取超时，未自动重试。") from None
        except Exception:
            raise SearchError("connection_failed", "无法安全连接搜索服务，未自动重试。") from None
        try:
            body = _strict_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise SearchError("invalid_response", "搜索服务返回了无效的 JSON 响应。") from None
        if not isinstance(body, dict) or body.get("error") is not None or body.get("type", "search") != "search":
            raise SearchError("invalid_response", "搜索服务返回的响应结构无效。")
        web = body.get("web")
        if web is None:
            # The API permits absent/null web results but specifies query/type in a normal response.
            if (body.get("type") == "search" and isinstance(body.get("query"), dict)
                    and isinstance(body["query"].get("original"), str)):
                return []
            raise SearchError("invalid_response", "搜索服务返回的响应结构无效。")
        if not isinstance(web, dict) or not isinstance(web.get("results"), list):
            raise SearchError("invalid_response", "搜索服务返回的网页结果结构无效。")
        results, seen = [], set()
        for item in web["results"]:
            if (not isinstance(item, dict) or not isinstance(item.get("title"), str)
                    or not isinstance(item.get("url"), str)
                    or (item.get("description") is not None and not isinstance(item["description"], str))):
                raise SearchError("invalid_response", "搜索服务返回的网页条目结构无效。")
            url = item["url"]
            try:
                parts = urlsplit(url)
                safe = (parts.scheme in {"http", "https"} and bool(parts.hostname)
                        and parts.username is None and parts.password is None
                        and not any(ord(char) <= 32 or ord(char) == 127 for char in url))
                parts.port  # Reject malformed ports without connecting to any result.
            except ValueError:
                safe = False
            if not safe or url in seen:
                continue
            seen.add(url)
            results.append({"title": item["title"], "url": url, "description": item.get("description") or ""})
        return results[:limit]
