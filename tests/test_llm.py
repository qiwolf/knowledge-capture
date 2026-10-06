import json
from unittest.mock import Mock, patch

import pytest
import requests

from knowledge_capture.llm import CloudClient, LLMError


def reply(content='{"summary":"测试结果"}', finish="stop", **message_extra):
    return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content, **message_extra}}]}


def fake_session(data=None, status=200, raw=None, headers=None, chunks=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.iter_content.return_value = chunks if chunks is not None else [raw if raw is not None else json.dumps(data if data is not None else reply()).encode()]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.post.return_value = response
    return session


@pytest.fixture
def client():
    return CloudClient("https://model.example/v1", "explicit-model", "secret-test-key")


def test_from_env_only_explicit_configuration():
    config = {"KC_LLM_BASE_URL": "https://model.example/v1/", "KC_LLM_MODEL": "chosen", "KC_LLM_API_KEY": "private"}
    with patch.dict("os.environ", config, clear=True):
        client = CloudClient.from_env()
    assert client.identity == {"provider": "model.example", "model": "chosen"}
    assert "private" not in repr(client.identity)
    with patch.dict("os.environ", {"OPENAI_API_KEY": "must-not-use"}, clear=True):
        with pytest.raises(LLMError) as error:
            CloudClient.from_env()
    assert error.value.code == "configuration_missing"


@pytest.mark.parametrize("url", ["http://model.example", "https://user:pass@model.example", "https://model.example?key=secret", "https://model.example#secret", "https://model.example?", "https://model.example#", "https://model.example\n", "https://", "https://model.example:bad"])
def test_invalid_endpoint_rejected(url):
    with pytest.raises(LLMError) as error:
        CloudClient(url, "model", "key")
    assert error.value.code == "configuration_invalid"


def test_request_sends_only_supplied_data_and_json_instruction(client):
    session = fake_session()
    with patch("knowledge_capture.llm.requests.Session", return_value=session):
        result = client.complete_json("请概括资料", {"text": "用户提供的内容"})
    assert result == {"summary": "测试结果"}
    assert session.trust_env is False
    session.post.assert_called_once()
    args, kwargs = session.post.call_args
    assert args == ("https://model.example/v1/chat/completions",)
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == (10, 180)
    assert kwargs["headers"]["Authorization"] == "Bearer secret-test-key"
    body = kwargs["json"]
    assert set(body) == {"model", "messages", "response_format", "max_completion_tokens"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 4096
    assert "JSON" in body["messages"][0]["content"]
    assert body["messages"][0]["content"].startswith("请概括资料")
    assert json.loads(body["messages"][1]["content"]) == {"text": "用户提供的内容"}


@pytest.mark.parametrize("status,code", [(301, "redirect_rejected"), (307, "redirect_rejected"), (401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (500, "http_error")])
def test_http_errors_never_expose_body_or_retry(client, status, code):
    session = fake_session(status=status, raw=b"secret-test-key server diagnostics")
    with patch("knowledge_capture.llm.requests.Session", return_value=session):
        with pytest.raises(LLMError) as error:
            client.complete_json("system", {})
    assert error.value.code == code
    assert "secret-test-key" not in str(error.value)
    session.post.assert_called_once()
    session.post.return_value.iter_content.assert_not_called()


@pytest.mark.parametrize("exc,code", [(requests.exceptions.Timeout("secret-test-key"), "timeout"), (requests.exceptions.ConnectionError("secret-test-key"), "connection_failed")])
def test_transport_failure_no_retry_or_secret(client, exc, code):
    session = fake_session()
    session.post.side_effect = exc
    with patch("knowledge_capture.llm.requests.Session", return_value=session):
        with pytest.raises(LLMError) as error:
            client.complete_json("system", {})
    assert error.value.code == code
    assert "secret-test-key" not in str(error.value)
    session.post.assert_called_once()


@pytest.mark.parametrize("data,code", [
    (reply(finish="length"), "truncated"),
    (reply(finish="content_filter"), "refused"),
    (reply(refusal="private server reason"), "refused"),
    (reply(finish=None), "incomplete"),
    (reply(tool_calls=[{"name": "execute"}]), "unsupported_tool_call"),
    (reply(function_call={"name": "execute"}), "unsupported_tool_call"),
    ({"choices": []}, "invalid_response"),
    ({"choices": [{"message": None}]}, "invalid_response"),
    ({"error": {"message": "private"}}, "invalid_response"),
    (reply(content=None), "invalid_response"),
    (reply(content="[]"), "invalid_json"),
    (reply(content="not JSON"), "invalid_json"),
    (reply(content='{"value":NaN}'), "invalid_json"),
    (reply(content='{"value":Infinity}'), "invalid_json"),
    (reply(content='{"value":1e400}'), "invalid_json"),
    (reply(content='{"key":1,"key":2}'), "invalid_json"),
    (reply(content='{"nested":{"key":1,"key":2}}'), "invalid_json"),
])
def test_incomplete_or_invalid_output(client, data, code):
    with patch("knowledge_capture.llm.requests.Session", return_value=fake_session(data=data)):
        with pytest.raises(LLMError) as error:
            client.complete_json("system", {})
    assert error.value.code == code
    assert "private" not in str(error.value)


@pytest.mark.parametrize("raw", [b"not json", b'{"choices":[],"choices":[]}', b'{"choices":NaN}', b"\xff"])
def test_invalid_envelope(client, raw):
    with patch("knowledge_capture.llm.requests.Session", return_value=fake_session(raw=raw)):
        with pytest.raises(LLMError) as error:
            client.complete_json("system", {})
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("declared", [True, False])
def test_response_size_limit(client, declared):
    session = fake_session(headers={"Content-Length": str(client.RESPONSE_LIMIT + 1)} if declared else {}, chunks=[b"x" * client.RESPONSE_LIMIT, b"x"])
    with patch("knowledge_capture.llm.requests.Session", return_value=session):
        with pytest.raises(LLMError) as error:
            client.complete_json("system", {})
    assert error.value.code == "response_too_large"


@pytest.mark.parametrize("payload", [{"value": float("nan")}, {"value": object()}, ["not-an-object"]])
def test_invalid_input_rejected_before_network(client, payload):
    with patch("knowledge_capture.llm.requests.Session") as session:
        with pytest.raises(LLMError) as error:
            client.complete_json("system", payload)
    assert error.value.code == "invalid_input"
    session.assert_not_called()


def test_configurable_wait_reaches_transport_and_still_no_retry():
    client = CloudClient('https://model.example/v1', 'model', 'key', timeout_seconds=300)
    session = fake_session()
    session.post.side_effect = requests.exceptions.ReadTimeout('provider detail')
    with patch('knowledge_capture.llm.requests.Session', return_value=session):
        with pytest.raises(LLMError) as exc:
            client.complete_json('system', {})
    assert exc.value.code == 'timeout'
    assert session.post.call_args.kwargs['timeout'] == (10, 300)
    session.post.assert_called_once()


@pytest.mark.parametrize('value', [9, 601, True, 180.5, '180', None])
def test_wait_validation(value):
    with pytest.raises(LLMError) as exc:
        CloudClient('https://model.example/v1', 'model', 'key', timeout_seconds=value)
    assert exc.value.code == 'configuration_invalid'


def test_environment_response_wait():
    env = {'KC_LLM_BASE_URL':'https://model.example/v1', 'KC_LLM_MODEL':'model', 'KC_LLM_API_KEY':'private', 'KC_LLM_TIMEOUT_SECONDS':'240'}
    with patch.dict('os.environ', env, clear=True):
        assert CloudClient.from_env().timeout_seconds == 240
    env['KC_LLM_TIMEOUT_SECONDS'] = '6.0'
    with patch.dict('os.environ', env, clear=True), pytest.raises(LLMError):
        CloudClient.from_env()
