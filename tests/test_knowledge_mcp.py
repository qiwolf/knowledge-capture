import hashlib
import json
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from knowledge_capture.knowledge_mcp import create_server, ReadOnlyStore, LIMIT_MARKDOWN
from knowledge_capture.store import Store
from test_portable import library
from test_wiki import capture


def invoke(store, name, arguments):
    async def call():
        async with create_connected_server_and_client_session(create_server(store)) as client:
            result = await client.call_tool(name, arguments)
            return result
    return anyio.run(call)


def body(result):
    return result.structuredContent or json.loads(result.content[0].text)


def fingerprint(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_real_sdk_protocol_tools_are_read_only_and_no_library_changes(tmp_path):
    store, source, analysis, topic, _ = library(tmp_path / 'library')
    (store.root / '.settings.json').write_text('PRIVATE-KEY')
    (store.root / 'providers.json').write_text('PRIVATE-CONFIG')
    before = fingerprint(store.root)
    async def run():
        async with create_connected_server_and_client_session(create_server(store)) as client:
            tools = (await client.list_tools()).tools
            assert {'knowledge_search', 'knowledge_read_source', 'knowledge_list_topics', 'knowledge_read_wiki','knowledge_create_note','knowledge_revise','knowledge_label','knowledge_reindex'} <= {t.name for t in tools}
            assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools if t.name not in {'knowledge_create_note','knowledge_revise','knowledge_label','knowledge_reindex'})
            assert all(not t.annotations.readOnlyHint and t.annotations.idempotentHint for t in tools if t.name in {'knowledge_create_note','knowledge_revise','knowledge_label','knowledge_reindex'})
            calls = [('knowledge_search', {'query': '备份', 'limit': 5}), ('knowledge_read_source', {'source_id': source['source_id']}),
                     ('knowledge_list_topics', {}), ('knowledge_read_wiki', {'topic_id': topic})]
            results = []
            for name, args in calls:
                response = await client.call_tool(name, args)
                assert not response.isError
                value = body(response)
                assert 'error' not in value, value
                results.append(value)
            return results
    results = anyio.run(run)
    packed = json.dumps(results, ensure_ascii=False)
    assert str(store.root) not in packed
    assert 'PRIVATE-KEY' not in packed and 'PRIVATE-CONFIG' not in packed
    assert results[0]['results'][0]['source_id'] == source['source_id']
    assert results[1]['analysis']['analysis_id'] == analysis['id']
    assert results[2]['topics'][0]['topic_id'] == topic
    assert results[3]['stale'] is False
    assert results[3]['sources'][0]['original_url'] == 'https://example.org/portable'
    assert before == fingerprint(store.root)


def test_source_only_library_read_does_not_create_analysis_schema(tmp_path):
    store = Store(tmp_path)
    source = store.ingest('https://example.org/a', capture_fn=capture('只有来源，还没有分析。'))
    before = fingerprint(store.root)
    answer = body(invoke(store, 'knowledge_read_source', {'source_id': source['source_id']}))
    assert answer['analysis'] is None
    assert body(invoke(store, 'knowledge_list_topics', {}))['topics'] == []
    assert before == fingerprint(store.root)


def test_exact_history_and_staleness(tmp_path):
    store, source, _, topic, wiki = library(tmp_path)
    store.ingest('https://example.org/portable', capture_fn=capture('这是新的来源版本。'))
    old = body(invoke(store, 'knowledge_read_source', {'source_id': source['source_id'], 'version': source['version_id']}))
    assert old['source']['version_id'] == source['version_id']
    assert old['is_latest_version'] is False
    assert old['analysis']['stale'] is True
    historic = body(invoke(store, 'knowledge_read_wiki', {'topic_id': topic, 'version': wiki['version_id']}))
    assert historic['version_id'] == wiki['version_id']
    assert historic['stale'] is True
    assert historic['sources'][0]['version_id'] == source['version_id']


def test_bounded_markdown_marks_truncation(tmp_path):
    store = Store(tmp_path)
    source = store.ingest('https://example.org/long', capture_fn=capture('中文资料' * 16000))
    answer = body(invoke(store, 'knowledge_read_source', {'source_id': source['source_id']}))
    assert answer['truncated']
    assert answer['truncations'][0]['field'] == 'markdown'
    assert len(answer['markdown']) < LIMIT_MARKDOWN + 100
    assert answer['next_offset'] == LIMIT_MARKDOWN
    assert answer['total_characters'] > LIMIT_MARKDOWN


def test_strict_schema_and_sanitized_missing_source(tmp_path):
    store = Store(tmp_path)
    assert invoke(store, 'knowledge_search', {'query': 'x', 'limit': 21}).isError
    assert invoke(store, 'knowledge_search', {'query': 'x', 'limit': True}).isError
    invalid = invoke(store, 'knowledge_read_source', {'source_id': '../PRIVATE-SECRET/settings.json'})
    assert invalid.isError
    assert 'PRIVATE-SECRET' not in str(invalid.content)
    result = body(invoke(store, 'knowledge_read_source', {'source_id': 'a' * 24}))
    assert result['error']['code'] == 'knowledge_unavailable'
    assert str(store.root) not in json.dumps(result)


def test_db_write_blocked(tmp_path):
    store = Store(tmp_path)
    import sqlite3
    import pytest
    connection = ReadOnlyStore(store)._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE sources SET title='not allowed'")
    finally:
        connection.close()


def test_source_pagination_reconstructs_cleaned_markdown(tmp_path):
    store = Store(tmp_path)
    content = ('中文与emoji🌏以及跨页引用。\n' * 4000) + str(store.root) + '/source'
    source = store.ingest('https://example.org/pages', capture_fn=capture(content))
    expected = store.read(source['source_id'])['markdown'].replace(str(store.root), '[knowledge-library]')
    async def run():
        pages, offset = [], 0
        async with create_connected_server_and_client_session(create_server(store)) as client:
            while True:
                response = await client.call_tool('knowledge_read_source', {'source_id': source['source_id'], 'version': source['version_id'], 'offset': offset, 'limit_chars': 11111})
                page = body(response)
                assert 'error' not in page
                assert page['total_characters'] == len(expected)
                assert page['offset'] == offset
                assert len(page['markdown']) <= 11111
                assert page['source']['version_id'] == source['version_id']
                if offset:
                    assert page['analysis'] is None and not page['analysis_included']
                pages.append(page['markdown'])
                if page['next_offset'] is None:
                    break
                assert page['truncated']
                offset = page['next_offset']
        return ''.join(pages)
    assert anyio.run(run) == expected
    for args in ({'offset': -1}, {'limit_chars': 0}, {'limit_chars': 48001}, {'offset': True}):
        assert invoke(store, 'knowledge_read_source', {'source_id': source['source_id'], **args}).isError
    beyond = body(invoke(store, 'knowledge_read_source', {'source_id': source['source_id'], 'offset': len(expected) + 1}))
    assert beyond['error']['code'] == 'knowledge_unavailable'


def test_mcp_note_revise_and_conflict(tmp_path):
    store=Store(tmp_path/'mcp-library')
    create={'title':'测试记录','markdown':'中文证据','idempotency_key':'mcp-create','tags':['测试']}
    first=body(invoke(store,'knowledge_create_note',create))
    assert first['http_status']==201,first
    assert body(invoke(store,'knowledge_create_note',create))==first
    result=body(invoke(store,'knowledge_revise',{'record_id':first['record_id'],'expected_version':first['version_id'],'markdown':'修订证据','idempotency_key':'mcp-revise'}))
    assert result['http_status']==200,result
    stale=body(invoke(store,'knowledge_label',{'record_id':first['record_id'],'expected_version':first['version_id'],'status':'expired','idempotency_key':'mcp-label'}))
    assert stale['http_status']==409 and stale['error']['code']=='version_conflict'
    found=body(invoke(store,'knowledge_find',{'query':'修订','tags':['测试']}))
    assert found['results'][0]['record_id']==first['record_id']
