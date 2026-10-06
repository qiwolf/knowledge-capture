import json
from unittest.mock import Mock, patch

import httpx
import pytest

from knowledge_capture.connectors import Connector, ConnectorError, LIMIT, load_services


def config(**extra):
    return {"transport": "http_json", "endpoint": "https://service.example/api", **extra}


def fake_session(body=b'{"ok":true}', status=200):
    response = Mock(status_code=status, headers={})
    response.iter_content.return_value = [body]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session = Mock()
    session.post.return_value = response
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    return session


@pytest.mark.parametrize("extra", [
    {"endpoint": "http://10.0.0.2/api"},
    {"endpoint": "http://8.8.8.8/api", "allow_insecure_http": True},
    {"endpoint": "https://user:password@service.example/api"},
    {"endpoint": "https://service.example/api?key=private"},
    {"api_key": "private"}, {"headers_env": {"Host": "OVERRIDE"}},
    {"transport": "mcp"}, {"timeout_seconds": 0},
])
def test_bad_configuration(extra):
    with pytest.raises(ConnectorError) as error:
        Connector(config(**extra))
    assert error.value.code == "configuration_invalid"


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.1.1", "[::1]", "[fd00::1]", "localhost"])
def test_explicit_local_http_allowed(host):
    assert Connector(config(endpoint=f"http://{host}:8080/api", allow_insecure_http=True))


def test_load_explicit_services_without_reading_tokens(tmp_path):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": {"search": config(token_env="UNSET_TEST_SECRET")}}))
    with patch("knowledge_capture.connectors.os.environ.get", side_effect=AssertionError("unexpected secret lookup")):
        assert load_services(path)["search"]["token_env"] == "UNSET_TEST_SECRET"
        assert Connector.from_file(path, "search")


def test_http_payload_and_environment_headers():
    session = fake_session()
    connector = Connector(config(token_env="TEST_TOKEN", headers_env={"X-Subscription-Token": "TEST_OTHER"}))
    with patch.dict("os.environ", {"TEST_TOKEN": "private", "TEST_OTHER": "another"}, clear=True), patch("knowledge_capture.connectors.requests.Session", return_value=session):
        assert connector.call({"query": "test"}) == {"ok": True}
    args, kwargs = session.post.call_args
    assert args == ("https://service.example/api",)
    assert kwargs["json"] == {"query": "test"}
    assert kwargs["headers"] == {"Authorization": "Bearer private", "X-Subscription-Token": "another"}
    assert kwargs["allow_redirects"] is False
    assert session.trust_env is False
    session.post.assert_called_once()


@pytest.mark.parametrize("body,code", [(b"not JSON", "invalid_response"), (b'{"x":1,"x":2}', "invalid_response"), (b"null", "invalid_response"), (b"x" * (LIMIT + 1), "response_too_large")])
def test_http_invalid_response(body, code):
    with patch("knowledge_capture.connectors.requests.Session", return_value=fake_session(body)):
        with pytest.raises(ConnectorError) as error:
            Connector(config()).call({})
    assert error.value.code == code


@pytest.mark.parametrize("status,code", [(307, "redirect_rejected"), (401, "authentication_failed"), (429, "rate_limited"), (500, "http_error")])
def test_http_error_safe_no_retry(status, code):
    session = fake_session(b"private-secret-body", status)
    with patch("knowledge_capture.connectors.requests.Session", return_value=session):
        with pytest.raises(ConnectorError) as error:
            Connector(config()).call({})
    assert error.value.code == code
    assert "private" not in str(error.value)
    session.post.assert_called_once()


def mcp_mock(result, seen, redirect=False):
    async def handle(request):
        seen.append(request)
        if redirect:
            return httpx.Response(307, headers={"Location": "https://other.example/mcp"})
        message = json.loads(request.content)
        if message["method"] == "initialize":
            value = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock", "version": "1"}}
        elif message["method"] == "notifications/initialized":
            return httpx.Response(202)
        elif message["method"] == "tools/call":
            value = result
        else:
            raise AssertionError("Unexpected MCP method")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": value})
    return httpx.MockTransport(handle)


@pytest.mark.parametrize("result,expected", [
    ({"content": [], "structuredContent": {"items": [1]}, "isError": False}, {"items": [1]}),
    ({"content": [{"type": "text", "text": '[{"title":"hello"}]'}]}, [{"title": "hello"}]),
])
def test_real_mcp_sdk_over_mock_transport(result, expected):
    seen = []
    with patch("httpx.AsyncHTTPTransport", return_value=mcp_mock(result, seen)):
        answer = Connector(config(transport="mcp", mcp_tool="configured_tool")).call({"url": "https://article.example"})
    assert answer == expected
    methods = [json.loads(request.content)["method"] for request in seen]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    call = json.loads(seen[-1].content)
    assert call["params"] == {"name": "configured_tool", "arguments": {"url": "https://article.example"}}
    assert seen[-1].headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("result,code", [
    ({"content": [{"type": "text", "text": "private failure"}], "isError": True}, "tool_error"),
    ({"content": [{"type": "text", "text": "not json"}]}, "invalid_response"),
    ({"content": [{"type": "text", "text": '{"x":1,"x":2}'}]}, "invalid_response"),
])
def test_mcp_error_payloads(result, code):
    with patch("httpx.AsyncHTTPTransport", return_value=mcp_mock(result, [])):
        with pytest.raises(ConnectorError) as error:
            Connector(config(transport="mcp", mcp_tool="tool")).call({})
    assert error.value.code == code
    assert "private" not in str(error.value)


def test_mcp_redirect_not_followed():
    seen = []
    with patch("httpx.AsyncHTTPTransport", return_value=mcp_mock({}, seen, redirect=True)):
        with pytest.raises(ConnectorError) as error:
            Connector(config(transport="mcp", mcp_tool="tool", timeout_seconds=1)).call({})
    assert len(seen) == 1
    assert error.value.code == "redirect_rejected"


def test_missing_secret_never_connects():
    with patch.dict("os.environ", {}, clear=True), patch("knowledge_capture.connectors.requests.Session") as session:
        with pytest.raises(ConnectorError) as error:
            Connector(config(token_env="MISSING_SECRET")).call({})
    assert error.value.code == "credential_missing"
    session.assert_not_called()


def test_http_array_response():
    with patch("knowledge_capture.connectors.requests.Session", return_value=fake_session(b"[1,2]")):
        assert Connector(config()).call({}) == [1, 2]


def test_mcp_bearer_and_size_guard():
    seen = []
    value = {"content": [{"type": "text", "text": "x" * LIMIT}]}
    with patch.dict("os.environ", {"TOKEN_TEST": "private-value"}, clear=True), patch("httpx.AsyncHTTPTransport", return_value=mcp_mock(value, seen)):
        with pytest.raises(ConnectorError) as error:
            Connector(config(transport="mcp", mcp_tool="tool", token_env="TOKEN_TEST")).call({})
    assert error.value.code == "response_too_large"
    assert len(seen) == 3
    assert all(request.headers["Authorization"] == "Bearer private-value" for request in seen)


def test_http_get_params_and_text_output():
    body = "# 文章标题\n\n保留原文。\n"
    session = fake_session(body.encode())
    session.get.return_value = session.post.return_value
    with patch("knowledge_capture.connectors.requests.Session", return_value=session):
        result = Connector(config(method="GET", response_format="text")).call({"url": "https://example.org/a", "q": "词语"})
    assert result == {"text": body}
    assert session.get.call_args.kwargs["params"] == {"url": "https://example.org/a", "q": "词语"}
    assert "json" not in session.get.call_args.kwargs
    session.post.assert_not_called()


@pytest.mark.parametrize("extra", [{"method": "PATCH"}, {"response_format": "guess"}, {"transport": "mcp", "mcp_tool": "tool", "method": "GET"}])
def test_invalid_explicit_modes(extra):
    with pytest.raises(ConnectorError):
        Connector(config(**extra))


def test_mcp_text_multiblock_preserved():
    value = {"content": [{"type": "text", "text": "# 标题"}, {"type": "text", "text": "正文\n"}], "structuredContent": {"ignored": "only in text mode"}}
    with patch("httpx.AsyncHTTPTransport", return_value=mcp_mock(value, [])):
        result = Connector(config(transport="mcp", mcp_tool="tool", response_format="text")).call({})
    assert result == {"text": "# 标题\n正文\n"}


def test_mcp_text_mode_requires_text():
    value = {"content": [], "structuredContent": {"data": "structured only"}}
    with patch("httpx.AsyncHTTPTransport", return_value=mcp_mock(value, [])):
        with pytest.raises(ConnectorError) as error:
            Connector(config(transport="mcp", mcp_tool="tool", response_format="text")).call({})
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("body,code", [(b"\xff", "invalid_response"), (b"x" * (LIMIT + 1), "response_too_large")])
def test_http_text_guards(body, code):
    with patch("knowledge_capture.connectors.requests.Session", return_value=fake_session(body)):
        with pytest.raises(ConnectorError) as error:
            Connector(config(response_format="text")).call({})
    assert error.value.code == code


@pytest.mark.parametrize('body,expected', [
    (b'{"opaque":{"stuff":[1]}}', {'opaque': {'stuff': [1]}}),
    (b'[{"title":"a"}]', [{'title': 'a'}]),
    (b'<html><body>full article</body></html>', {'text': '<html><body>full article</body></html>'}),
    ('完整的纯文本内容'.encode(), {'text': '完整的纯文本内容'}),
])
def test_auto_http_preserves_json_html_and_plaintext(body, expected):
    with patch('knowledge_capture.connectors.requests.Session', return_value=fake_session(body)):
        assert Connector(config(response_format='auto')).call({}) == expected


def test_auto_http_queued_is_not_completed_content():
    with patch('knowledge_capture.connectors.requests.Session', return_value=fake_session(b'{"job":"accepted"}', 202)):
        with pytest.raises(ConnectorError) as caught:
            Connector(config(response_format='auto')).call({})
    assert caught.value.code == 'not_ready'


def test_auto_mcp_preserves_all_content_alongside_structured():
    value = {'content': [{'type': 'text', 'text': 'first part'}, {'type': 'text', 'text': '<p>second part</p>'}],
             'structuredContent': {'article': {'title': 'Title', 'text': 'structured body'}}}
    with patch('httpx.AsyncHTTPTransport', return_value=mcp_mock(value, [])):
        result = Connector(config(transport='mcp', mcp_tool='reader', response_format='auto')).call({})
    assert result['structuredContent'] == value['structuredContent']
    assert [block['text'] for block in result['content']] == ['first part', '<p>second part</p>']
