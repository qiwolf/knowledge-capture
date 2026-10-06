import json
from unittest.mock import Mock, patch

import pytest
import requests

from knowledge_capture.search import SearchClient, SearchError


def fake_session(body=None, status=200, raw=None, headers=None, chunks=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.iter_content.return_value = chunks if chunks is not None else [raw if raw is not None else json.dumps(body).encode()]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response
    return session


def result(url="https://public.example/a", **extra):
    return {"title": "测试资料", "url": url, "description": "有效摘要", **extra}


@pytest.fixture
def client():
    return SearchClient("secret-test-key")


def test_explicit_environment_only():
    with patch.dict("os.environ", {"KC_SEARCH_PROVIDER": "brave", "KC_SEARCH_API_KEY": "key"}, clear=True), patch("knowledge_capture.search.requests.Session") as session:
        assert isinstance(SearchClient.from_env(), SearchClient)
        session.assert_not_called()
    with patch.dict("os.environ", {"BRAVE_API_KEY": "must-not-use"}, clear=True):
        with pytest.raises(SearchError):
            SearchClient.from_env()


@pytest.mark.parametrize("provider,key,code", [("", "key", "configuration_invalid"), ("other", "key", "configuration_invalid"), ("brave", "", "configuration_missing")])
def test_configuration_errors(provider, key, code):
    with pytest.raises(SearchError) as error:
        SearchClient(key, provider)
    assert error.value.code == code


def test_contract_deduplication_filtering_and_limit(client):
    data = {"web": {"results": [result(), result(), result("file:///etc/passwd"), result("https://user:secret@example.org"), result("http://example.org/second", description=None), result("https://example.org/third")]}}
    session = fake_session(data)
    with patch("knowledge_capture.search.requests.Session", return_value=session):
        found = client.search("知识采集", limit=2)
    assert found == [result(), result("http://example.org/second", description="")]
    session.get.assert_called_once()
    args, kwargs = session.get.call_args
    assert args == ("https://api.search.brave.com/res/v1/web/search",)
    assert kwargs["params"] == {"q": "知识采集", "count": 2}
    assert kwargs["headers"]["X-Subscription-Token"] == "secret-test-key"
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == (10, 30)
    assert session.trust_env is False


@pytest.mark.parametrize("body", [{"web": {"results": []}}, {"type": "search", "query": {"original": "query"}}, {"type": "search", "query": {"original": "query"}, "web": None}])
def test_valid_empty_results(client, body):
    with patch("knowledge_capture.search.requests.Session", return_value=fake_session(body)):
        assert client.search("query") == []


@pytest.mark.parametrize("body", [{}, [], {"error": {"message": "secret-test-key"}}, {"type": "ErrorResponse"}, {"web": []}, {"web": {}}, {"web": {"results": None}}, {"web": {"results": [None]}}, {"web": {"results": [{"title": "missing URL"}]}}, {"web": {"results": [result(description=7)]}}])
def test_malformed_is_not_empty(client, body):
    with patch("knowledge_capture.search.requests.Session", return_value=fake_session(body)):
        with pytest.raises(SearchError) as error:
            client.search("query")
    assert error.value.code == "invalid_response"
    assert "secret-test-key" not in str(error.value)


@pytest.mark.parametrize("status,code", [(301, "redirect_rejected"), (307, "redirect_rejected"), (401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (500, "service_unavailable"), (503, "service_unavailable"), (422, "http_error")])
def test_http_errors_are_distinct_safe_and_not_retried(client, status, code):
    session = fake_session(status=status, raw=b"secret-test-key")
    with patch("knowledge_capture.search.requests.Session", return_value=session):
        with pytest.raises(SearchError) as error:
            client.search("query")
    assert error.value.code == code
    assert "secret-test-key" not in str(error.value)
    session.get.assert_called_once()
    session.get.return_value.iter_content.assert_not_called()


@pytest.mark.parametrize("raw", [b"bad JSON", b'{"web":{},"web":{}}', b'{"web":NaN}', b"\xff"])
def test_invalid_json(client, raw):
    with patch("knowledge_capture.search.requests.Session", return_value=fake_session(raw=raw)):
        with pytest.raises(SearchError) as error:
            client.search("query")
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("declared", [True, False])
def test_size_limit(client, declared):
    session = fake_session(headers={"Content-Length": str(client.RESPONSE_LIMIT + 1)} if declared else {}, chunks=[b"x" * client.RESPONSE_LIMIT, b"x"])
    with patch("knowledge_capture.search.requests.Session", return_value=session):
        with pytest.raises(SearchError) as error:
            client.search("query")
    assert error.value.code == "response_too_large"


@pytest.mark.parametrize("exception,code", [(requests.exceptions.Timeout("secret-test-key"), "timeout"), (requests.exceptions.ConnectionError("secret-test-key"), "connection_failed")])
def test_transport_errors(client, exception, code):
    session = fake_session()
    session.get.side_effect = exception
    with patch("knowledge_capture.search.requests.Session", return_value=session):
        with pytest.raises(SearchError) as error:
            client.search("query")
    assert error.value.code == code
    assert "secret-test-key" not in str(error.value)
    session.get.assert_called_once()


@pytest.mark.parametrize("query,limit", [("", 5), ("x" * 601, 5), ("x " * 76, 5), ("query", 0), ("query", 21), ("query", True)])
def test_invalid_input_before_network(client, query, limit):
    with patch("knowledge_capture.search.requests.Session") as session:
        with pytest.raises(SearchError) as error:
            client.search(query, limit)
    assert error.value.code == "invalid_input"
    session.assert_not_called()
