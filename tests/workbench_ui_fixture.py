"""Offline UI acceptance fixture. Not a real model/search/provider acceptance.

Start explicitly: .venv/bin/python tests/workbench_ui_fixture.py
Only data/ui-acceptance is used. Stop with Ctrl-C. Browser address: port 8877.
All outbound socket connections are blocked within this process. Test doubles
exist only while this script runs; production modules and other libraries remain
unchanged. UI writes persist inside the clearly marked demonstration library.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
from unittest.mock import patch
import zlib

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from knowledge_capture.capture import CaptureError
from knowledge_capture.context_alerts import ContextAlerts
from knowledge_capture.gateway import create_server
from knowledge_capture.llm import CloudClient
from knowledge_capture.processing import Processor
from knowledge_capture.providers import CaptureRouter, ConfiguredSearch
from knowledge_capture.settings import Settings
from knowledge_capture.store import Store, source_id
from knowledge_capture.wiki import Wiki

LABEL = '【演示测试】'
TOPIC = LABEL + '路由器维护'
BASE_URL = 'https://ui-demo.example/articles/'
MARKER = '.ui-fixture.json'


def png_fixture():
    """Small valid RGB chart; fixture pixels, never a remotely fetched image."""
    width, height = 320, 160
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            color = (238, 244, 247)
            for left, top, rgb in [(40, 90, (33, 111, 146)), (120, 55, (49, 153, 136)), (200, 25, (220, 151, 66))]:
                if left <= x < left + 50 and top <= y < 140:
                    color = rgb
            row.extend(color)
        rows.append(b'\x00' + bytes(row))
    def chunk(name, data):
        return struct.pack('!I', len(data)) + name + data + struct.pack('!I', zlib.crc32(name + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')


def fixture_capture(url, directory):
    endings = {
        '1': ('版本核对', '路由器维护首先核对设备当前版本，演示设备的版本信息仅用于测试。'),
        '2': ('配置备份', '升级前需要导出配置并验证备份可读取，这是一段用于界面测试的示例材料。'),
        '3': ('兼容性检查', '不同版本升级前应检查插件兼容条件；这里没有真实安全漏洞声明。'),
        '4': ('新增回滚演练', '新增资料提出升级前演练回滚流程并记录恢复耗时，仅为主动检索演示。'),
        'manual': ('手动投递', '这条资料用于验证浏览器投递和后续分析按钮，全部内容均为演示测试。'),
    }
    suffix = url.removeprefix(BASE_URL)
    if not url.startswith(BASE_URL) or suffix not in endings:
        raise CaptureError('fixture_only', LABEL + '夹具仅接受 ui-demo.example/articles/1、2、3、4 或 manual；未访问外部网络。')
    title, paragraph = endings[suffix]
    image = png_fixture()
    (directory / 'demo-chart.png').write_bytes(image)
    return {'title': LABEL + title, 'markdown': f'{LABEL}{paragraph}\n\n## {LABEL}离线图片\n\n![{LABEL}三阶段示意图](assets/demo-chart.png)\n\n此图片和文字均为本地夹具，不代表真实采集或模型判断。',
            'original_url': url, 'final_url': url, 'author': LABEL + '夹具作者', 'published_at': None,
            'status': 'complete', 'warnings': [LABEL + '固定离线内容；不能作为真实服务验收'],
            'assets': [{'status': 'complete', 'relative_path': 'assets/demo-chart.png', 'sha256': hashlib.sha256(image).hexdigest()}],
            'acquisition': {'service': 'ui-fixture', 'transport': 'offline-fixture', 'capability': 'reader'}}


class FixtureModel:
    identity = {'provider': 'offline-ui-fixture', 'model': LABEL + '固定响应，非真实AI'}

    def complete_json(self, system, payload):
        if 'sources' in payload:
            source = payload['sources'][0]
            line = source['lines'][0]
            cite = {'source_id': source['source_id'], 'version_id': source['version_id'],
                    'start_line': line['number'], 'end_line': line['number'], 'quote': line['text']}
            if 'facts' in payload:
                return {'alerts': [{'title': LABEL + '核实演示路由器版本', 'detail': LABEL + '这是背景与资料的固定关联示例，不构成真实漏洞或升级建议。',
                                    'suggested_action': LABEL + '检查演示背景与引用；不要执行设备操作。',
                                    'fact_ids': [payload['facts'][0]['id']], 'evidence': [cite]}]}
            cite.pop('quote')
            return {'summary': [{'text': LABEL + '维护知识包括版本核对、备份和兼容性检查；固定综合仅用于UI验收。', 'evidence': [cite]}],
                    'agreements': [], 'differences': [], 'questions': []}
        lines = payload.get('candidate', payload)['lines']
        line = lines[0]
        cite = {'start_line': line['number'], 'end_line': line['number'], 'quote': line['text']}
        if 'candidate' in payload:
            return {'relevant': True, 'novel': True, 'reason': LABEL + '固定相关性和新增信息判定，非真实模型评价', 'evidence': [cite]}
        return {'summary': [{'text': LABEL + '固定摘要：核实版本与维护条件。', 'evidence': [cite]}],
                'key_points': [{'text': LABEL + '全部内容只用于演示。', 'evidence': [cite]}],
                'topics': [{'name': TOPIC, 'reason': LABEL + '三条独立示例共同支持维护主题', 'evidence': [cite]}], 'questions': []}


def fixture_search(self, query, limit=5):
    return [{'url': BASE_URL + '4', 'title': LABEL + '新增回滚演练',
             'description': LABEL + '固定搜索结果，未连接搜索服务；重复检索将按真实去重逻辑处理。'}][:limit]


@contextmanager
def offline_bindings(root):
    root = Path(root).resolve()
    def model_for_store(cls, store):
        if store.root.resolve() != root:
            raise RuntimeError('UI fixture refuses another library')
        return FixtureModel()
    def no_connect(*args, **kwargs):
        raise RuntimeError('UI fixture blocks all outbound socket connections')
    with ExitStack() as stack:
        stack.enter_context(patch.object(CloudClient, 'for_store', classmethod(model_for_store)))
        stack.enter_context(patch.object(ConfiguredSearch, 'search', fixture_search))
        stack.enter_context(patch.object(CaptureRouter, 'capture', lambda self, url, directory: fixture_capture(url, directory)))
        stack.enter_context(patch('knowledge_capture.capture.capture_url', fixture_capture))
        stack.enter_context(patch('knowledge_capture.discovery.capture_url', fixture_capture))
        stack.enter_context(patch.object(socket.socket, 'connect', no_connect))
        stack.enter_context(patch.object(socket.socket, 'connect_ex', no_connect))
        yield


def seed(root=None):
    root = Path(root or PROJECT / 'data/ui-acceptance').resolve()
    if root.name != 'ui-acceptance':
        raise ValueError('夹具目录必须明确命名 ui-acceptance')
    marker = root / MARKER
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError('拒绝接管没有演示标记的已有知识库')
    if marker.exists() and json.loads(marker.read_text()) != {'kind': 'offline-ui-acceptance', 'version': 1}:
        raise ValueError('演示标记不匹配')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'kind': 'offline-ui-acceptance', 'version': 1}), encoding='utf-8')
    store = Store(root)
    processor = Processor(store)
    known = {item['id'] for item in store.list_sources()}
    for number in range(1, 4):
        url = BASE_URL + str(number)
        sid = source_id(url)
        if sid not in known:
            store.ingest(url, note=LABEL + '预置独立资料', capture_fn=fixture_capture)
        version = store.read(sid)['metadata']['version_id']
        if not any(row['source_id'] == sid and row['source_version'] == version and row['status'] in {'complete', 'partial'} for row in processor.history()):
            processor.analyze(sid, FixtureModel())
    topic = next(t for t in processor.interests() if t['name'] == TOPIC)
    wiki = Wiki(store)
    if not any(page['topic_id'] == topic['id'] for page in wiki.list_pages()):
        wiki.build(topic['id'], FixtureModel())
    alerts = ContextAlerts(store)
    if not alerts.list_facts():
        alerts.set_fact(LABEL + '虚拟路由器', '版本', LABEL + '7.10（虚构设备）')
    if not alerts.list_alerts():
        alerts.analyze(client=FixtureModel())
    settings = Settings(store)
    if not settings.path.exists():
        settings.save_model('https://offline-ui-fixture.invalid/v1', LABEL + '固定离线模型', 'NOT-A-REAL-KEY-UI-FIXTURE')
    if not settings.providers_path.exists():
        settings.save_providers({'services': {'demo': {'transport': 'http_json', 'endpoint': 'https://offline-ui-fixture.invalid/service'}},
                                 'capabilities': {'search': {'service': 'demo'}, 'reader': {'service': 'demo'}},
                                 'routing': {'default_reader': 'reader'}})
    return store


def main():
    store = seed()
    with offline_bindings(store.root):
        server = create_server(store, host='127.0.0.1', port=8877, auto_process=False)
        print('http://127.0.0.1:8877/', flush=True)
        print(str(store.root / '.api-token'), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
