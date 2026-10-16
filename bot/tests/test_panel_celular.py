"""Modo celular: el panel en la red de casa, pero nadie entra sin la clave del QR."""
import http.client
import json
import threading

import pytest

from bot.dashboard import remote
from bot.dashboard.server import serve
from bot.tests.test_engine_paper import FakeAnalyst, make_engine, setup  # noqa: F401


class Client:
    def __init__(self, port):
        self.port = port

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"192.168.1.50:{self.port}", **(headers or {})}  # como lo abre el iPhone
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out


@pytest.fixture
def phone_server(setup, monkeypatch):
    s, states, ex, now = setup
    make_engine(s, states, ex, FakeAnalyst(), now).write_panel(now)
    phone = {"on": True}
    monkeypatch.setattr(remote, "is_loopback", lambda addr: not phone["on"])  # True = la petición viene de la PC
    servers = []

    def start(enabled):
        srv = serve(s, port=0, host="127.0.0.1", phone=enabled)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]
    yield s, start, phone
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_phone_is_locked_out_when_mode_is_off(phone_server):
    s, start, _ = phone_server
    port = start(False)
    status, _, body = Client(port).req("GET", "/")
    assert status == 401 and "QR" in body.decode()
    assert Client(port).req("GET", "/api/estado")[0] == 403
    assert not (s.storage.reports_dir / remote.FILENAME).exists()


def test_phone_enters_only_with_the_key(phone_server):
    s, start, phone = phone_server
    port = start(True)
    key = (s.storage.reports_dir / remote.FILENAME).read_text().strip()
    cl = Client(port)
    assert cl.req("GET", "/")[0] == 401
    assert cl.req("GET", "/?t=" + "x" * len(key))[0] == 401
    assert cl.req("GET", "/api/estado", {"Cookie": f"{remote.COOKIE}=nope"})[0] == 403
    status, headers, _ = cl.req("GET", "/?t=" + key)
    assert status == 302 and headers["Location"] == "/"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    ck = {"Cookie": headers["Set-Cookie"].split(";")[0]}
    assert cl.req("GET", "/", ck)[0] == 200
    assert json.loads(cl.req("GET", "/api/estado", ck)[2])["mode"] == s.mode.value
    assert cl.req("GET", "/api/acceso", ck)[0] == 403  # la clave solo se le muestra a la PC
    body = json.dumps({"mode": "solo_cierre"}).encode()
    post = {**ck, "Content-Type": "application/json", "Content-Length": str(len(body))}
    assert cl.req("POST", "/api/control", post, body)[0] == 403  # sin la cabecera del panel (otra web)
    status, _, out = cl.req("POST", "/api/control", {**post, "X-Jarvis-Panel": "1"}, body)
    assert status == 200 and json.loads(out)["mode"] == "solo_cierre"

    phone["on"] = False  # ahora desde la PC
    status, _, out = Client(port).req("GET", "/api/acceso", {"Host": f"127.0.0.1:{port}"})
    acc = json.loads(out)
    assert status == 200 and acc["enabled"] and all(key in u["url"] for u in acc["urls"])


def test_key_is_stable_and_random(tmp_path):
    k1 = remote.load_or_create_key(tmp_path)
    assert len(k1) >= 20 and remote.load_or_create_key(tmp_path) == k1
    assert remote.load_or_create_key(tmp_path / "otro") != k1
    assert remote.cookie_value(f"a=1; {remote.COOKIE}=abc; b=2") == "abc"
    assert not remote.key_ok("", k1) and remote.key_ok(k1, k1)
