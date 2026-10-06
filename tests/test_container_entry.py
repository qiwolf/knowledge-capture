import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_capture.container_entry import configuration
from knowledge_capture.gateway import create_server
from knowledge_capture.store import Store


def test_container_bind_requires_explicit_opt_in(tmp_path):
    store=Store(tmp_path/'data')
    with pytest.raises(ValueError): create_server(store,host='0.0.0.0',port=0)
    with pytest.raises(ValueError): create_server(store,host='192.0.2.1',port=0,container_network=True)
    server=create_server(store,host='0.0.0.0',port=0,container_network=True)
    try:
        assert server.server_address[0]=='0.0.0.0'
        assert (store.root/'.api-token').stat().st_mode & 0o777 == 0o600
    finally: server.server_close()


def test_entrypoint_stops_and_restores_signals(monkeypatch,tmp_path):
    import knowledge_capture.container_entry as entry
    import knowledge_capture.gateway as gateway
    handlers={}
    monkeypatch.setenv('KNOWLEDGE_DATA',str(tmp_path/'data'))
    monkeypatch.setattr(signal,'signal',lambda sig,fn: handlers.setdefault(sig,fn))
    server=Mock()
    server.store=SimpleNamespace(root=tmp_path/'data')
    def serve(): handlers[signal.SIGTERM](signal.SIGTERM,None)
    server.serve_forever.side_effect=serve
    monkeypatch.setattr(gateway,'create_server',lambda *args,**kwargs:server)
    assert entry.main()==0
    server.server_close.assert_called_once()
    # Shutdown executes on a separate thread, avoiding serve_forever deadlock.
    import time
    deadline=time.monotonic()+1
    while not server.shutdown.called and time.monotonic()<deadline: time.sleep(.001)
    server.shutdown.assert_called_once()


def test_data_path_must_be_absolute():
    with pytest.raises(ValueError): configuration({'KNOWLEDGE_DATA':'relative'})
    assert str(configuration({}))=='/data'


def test_container_listener_keeps_host_and_auth_gates(tmp_path):
    import threading
    import requests
    server=create_server(Store(tmp_path/'guarded'),host='0.0.0.0',port=0,container_network=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    session=requests.Session();session.trust_env=False
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        assert session.get(base+'/api/health').status_code==200
        assert session.get(base+'/api/v1/knowledge').status_code==401
        assert session.get(base+'/api/health',headers={'Host':f'example.org:{server.server_port}'}).status_code==403
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2);session.close()


def test_explicit_public_port_is_container_only_and_host_remains_loopback(tmp_path):
    from knowledge_capture.container_entry import public_port
    import threading
    import requests
    assert public_port({})==8765
    assert public_port({'KNOWLEDGE_PUBLIC_PORT':'18765'})==18765
    for value in ['0','65536','-1','bad','１２３']:
        with pytest.raises(ValueError): public_port({'KNOWLEDGE_PUBLIC_PORT':value})
    store=Store(tmp_path/'public-port')
    with pytest.raises(ValueError): create_server(store,port=0,public_port=18765)
    server=create_server(store,host='0.0.0.0',port=0,container_network=True,public_port=18765)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    session=requests.Session();session.trust_env=False
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        assert session.get(base+'/api/health',headers={'Host':'127.0.0.1:18765'}).status_code==200
        assert session.get(base+'/api/health',headers={'Host':'localhost:18765'}).status_code==200
        assert session.get(base+'/api/health').status_code==200
        assert session.get(base+'/api/health',headers={'Host':'example.org:18765'}).status_code==403
        assert session.get(base+'/api/health',headers={'Host':'127.0.0.1:18766'}).status_code==403
        assert session.get(base+'/api/v1/knowledge',headers={'Host':'127.0.0.1:18765'}).status_code==401
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2);session.close()
