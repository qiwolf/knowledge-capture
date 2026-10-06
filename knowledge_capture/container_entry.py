"""Explicit container entrypoint; browser Host and Bearer gates remain enforced."""
import json
import os
from pathlib import Path
import signal
import threading
import urllib.request


def configuration(environ=None):
    env = os.environ if environ is None else environ
    root = Path(env.get('KNOWLEDGE_DATA', '/data'))
    if not root.is_absolute():
        raise ValueError('KNOWLEDGE_DATA 必须为绝对路径。')
    return root


def public_port(environ=None):
    env = os.environ if environ is None else environ
    raw = env.get('KNOWLEDGE_PUBLIC_PORT', '8765')
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError('KNOWLEDGE_PUBLIC_PORT 必须为 1 至 65535 的端口。')
    return int(raw)


def healthcheck():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8765/api/health', timeout=5) as response:
        if response.status != 200 or json.load(response).get('status') != 'ok':
            raise RuntimeError('服务健康检查失败。')


def main():
    from .gateway import create_server
    from .store import Store
    os.umask(0o077)
    external_port = public_port()
    store = Store(configuration())
    server = create_server(store, host='0.0.0.0', port=8765, container_network=True, public_port=external_port)
    stopping = threading.Event()

    def stop(_signum, _frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, name='container-shutdown', daemon=True).start()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    print(json.dumps({'status':'listening','url':f'http://127.0.0.1:{external_port}','token_file':str(store.root / '.api-token')}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
