"""Explicitly configured OpenAI-compatible JSON client; never retries a POST.

Providers must support chat/completions, JSON mode and max_completion_tokens.
JSON mode does not validate the application's schema; callers must validate it.
"""
from __future__ import annotations

import json
import math
import os
from urllib.parse import urlsplit, urlunsplit

import requests


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("non-finite constant")

    def floating(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite number")
        return result

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)


class CloudClient:
    RESPONSE_LIMIT = 2 * 1024 * 1024

    def __init__(self, base_url: str, model: str, api_key: str, timeout_seconds: int = 180):
        if type(timeout_seconds) is not int or not 10 <= timeout_seconds <= 600:
            raise LLMError("configuration_invalid", "模型响应等待时间必须为10至600秒的整数。")
        self.timeout_seconds = timeout_seconds
        if not all(isinstance(value, str) and value.strip() for value in (base_url, model, api_key)):
            raise LLMError("configuration_missing", "请配置云端模型的服务地址、模型名称和 API 密钥。")
        try:
            parts = urlsplit(base_url)
            if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                    or parts.password is not None or parts.query or parts.fragment
                    or "?" in base_url or "#" in base_url
                    or any(ord(char) < 33 or ord(char) == 127 for char in base_url)):
                raise ValueError()
            host = parts.hostname.encode("idna").decode("ascii")
            port = parts.port
            netloc = f"[{host}]" if ":" in host else host
            if port is not None:
                netloc += f":{port}"
            self._endpoint = urlunsplit(("https", netloc, parts.path.rstrip("/") + "/chat/completions", "", ""))
        except (ValueError, UnicodeError):
            raise LLMError("configuration_invalid", "模型服务地址必须是无账号密码、查询参数和片段的 HTTPS 地址。") from None
        self._host = host
        self._model = model.strip()
        self._api_key = api_key.strip()

    @classmethod
    def from_env(cls):
        raw_timeout = os.environ.get("KC_LLM_TIMEOUT_SECONDS", "180")
        if not raw_timeout.isascii() or not raw_timeout.isdigit():
            raise LLMError("configuration_invalid", "模型响应等待时间必须为10至600秒的整数。")
        return cls(os.environ.get("KC_LLM_BASE_URL", ""), os.environ.get("KC_LLM_MODEL", ""),
                   os.environ.get("KC_LLM_API_KEY", ""), timeout_seconds=int(raw_timeout))

    @classmethod
    def for_store(cls, store):
        from .settings import Settings, SettingsError, ENV_MODEL
        # Any explicit environment setting selects that complete credential group.
        if any(name in os.environ for name in ENV_MODEL):
            return cls.from_env()
        try:
            model = Settings(store).local_model()
        except SettingsError:
            raise LLMError("configuration_invalid", "本地模型设置无效，请检查设置后重试。") from None
        if model is None:
            return cls.from_env()
        return cls(model['base_url'], model['model'], model['api_key'], timeout_seconds=model.get('timeout_seconds', 180))

    @property
    def identity(self) -> dict:
        return {"provider": self._host, "model": self._model}

    @property
    def cache_identity(self) -> dict:
        return {**self.identity, "endpoint": self._endpoint}

    def complete_json(self, system: str, payload: dict) -> dict:
        if not isinstance(system, str) or not isinstance(payload, dict):
            raise LLMError("invalid_input", "模型请求必须包含文本指令和 JSON 对象数据。")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise LLMError("invalid_input", "模型请求包含无法转换为 JSON 的数据。") from None
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system + "\n请仅返回一个有效的 JSON 对象，不要附加说明或 Markdown 围栏。"},
                {"role": "user", "content": encoded},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 4096,
        }
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.post(self._endpoint, json=body,
                                  headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
                                  timeout=(10, self.timeout_seconds), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise LLMError("redirect_rejected", "模型服务返回跳转，已停止请求以保护凭据。")
                    if status in {401, 403}:
                        raise LLMError("authentication_failed", "模型服务拒绝认证或当前账号没有访问权限。")
                    if status == 429:
                        raise LLMError("rate_limited", "模型服务额度不足或请求过于频繁，请稍后重试。")
                    if status != 200:
                        raise LLMError("http_error", "模型服务返回错误，未完成处理。")
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > self.RESPONSE_LIMIT:
                        raise LLMError("response_too_large", "模型返回内容超过大小限制。")
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > self.RESPONSE_LIMIT:
                            raise LLMError("response_too_large", "模型返回内容超过大小限制。")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except LLMError:
            raise
        except requests.exceptions.Timeout:
            raise LLMError("timeout", "模型服务连接或读取超时，未自动重试。") from None
        except Exception:
            raise LLMError("connection_failed", "无法安全连接模型服务，未自动重试。") from None
        try:
            envelope = _strict_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise LLMError("invalid_response", "模型服务返回了无效的 JSON 响应。") from None
        if not isinstance(envelope, dict) or envelope.get("error") is not None:
            raise LLMError("invalid_response", "模型服务返回的响应结构无效。")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise LLMError("invalid_response", "模型服务返回的响应结构无效。")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LLMError("invalid_response", "模型服务返回的消息结构无效。")
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise LLMError("refused", "模型拒绝处理这条资料。")
        if message.get("tool_calls") or message.get("function_call"):
            raise LLMError("unsupported_tool_call", "模型返回了不支持的工具调用。")
        if choice.get("finish_reason") == "length":
            raise LLMError("truncated", "模型返回内容被截断，未保存不完整结果。")
        if choice.get("finish_reason") != "stop":
            raise LLMError("incomplete", "模型未正常结束生成，未保存结果。")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMError("invalid_response", "模型未返回有效的文本结果。")
        try:
            result = _strict_json(content)
        except (ValueError, RecursionError):
            raise LLMError("invalid_json", "模型结果不是有效的 JSON，未保存结果。") from None
        if not isinstance(result, dict):
            raise LLMError("invalid_json", "模型结果必须是一个 JSON 对象。")
        return result
