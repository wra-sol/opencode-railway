import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest

import wizard

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_OPENCODE = """\
#!/usr/bin/env python3
import sys, os, json, base64, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT=None; HOST="127.0.0.1"
args=sys.argv[2:]
for i,a in enumerate(args):
    if a=="--port": PORT=int(args[i+1])
    if a=="--hostname": HOST=args[i+1]
PW=os.environ.get("OPENCODE_SERVER_PASSWORD","")
def ok_auth(h):
    v=h.get("Authorization","")
    if not v.startswith("Basic "): return False
    try: return base64.b64decode(v[6:]).decode()=="opencode:"+PW
    except Exception: return False
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        if self.path=="/global/health":
            # Real opencode requires auth even on /global/health when a password is set.
            if PW and not ok_auth(self.headers):
                self.send_response(401); self.send_header("Content-Length","0"); self.end_headers(); return
            b=json.dumps({"healthy":True,"version":"fake"}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
        if not ok_auth(self.headers):
            self.send_response(401); self.send_header("Content-Length","0"); self.end_headers(); return
        if self.path=="/event":
            self.send_response(200); self.send_header("Content-Type","text/event-stream")
            self.send_header("Cache-Control","no-store"); self.end_headers()
            for i in range(2):
                self.wfile.write(f"data: hello-{i}\\n\\n".encode()); self.wfile.flush(); time.sleep(0.02)
            return
        b=("FAKE UI "+self.path).encode()
        self.send_response(200); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        if not ok_auth(self.headers):
            self.send_response(401); self.send_header("Content-Length","0"); self.end_headers(); return
        ln=int(self.headers.get("Content-Length","0") or "0"); body=self.rfile.read(ln) if ln else b""
        b=("FAKE POST "+self.path+" "+body.decode("utf-8","replace")).encode()
        self.send_response(200); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer((HOST,PORT),H).serve_forever()
"""


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_ready(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            c.request("GET", "/health")
            c.getresponse().read()
            return True
        except Exception:
            time.sleep(0.15)
        finally:
            c.close()
    return False


def _start_manager(port, data_dir, fake_bin, env_overrides=None):
    env = dict(os.environ)
    env["PATH"] = fake_bin + os.pathsep + env["PATH"]
    env["PYTHONPATH"] = REPO
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "wizard.py"), "--port", str(port), "--data", data_dir, "--manage"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def _req(method, port, path, headers=None, body=None, raw=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    if raw:
        return r.status, dict(r.getheaders()), data
    return r.status, data.decode("utf-8", "replace")


@pytest.fixture
def fake_bin(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "opencode").write_text(FAKE_OPENCODE)
    os.chmod(d / "opencode", 0o755)
    return str(d)


@pytest.fixture
def configured_manager(tmp_path, fake_bin):
    port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    (data / ".setup.env").write_text(
        "OPENCODE_SERVER_PASSWORD=testpw\nANTHROPIC_API_KEY=fakekey\nOPENCODE_PROVIDER=anthropic\nOPENCODE_PROVIDER_KEY_ENV=ANTHROPIC_API_KEY\n"
    )
    os.chmod(data / ".setup.env", 0o600)
    proc = _start_manager(port, str(data), fake_bin)
    if not _wait_ready(port):
        proc.kill()
        out = proc.communicate(timeout=5)[0] or ""
        pytest.fail(f"manager did not start: {out}")
    # Wait for the child to be fully ready for proxying (not just "starting").
    # /health returns healthy during startup too; a successful proxied GET means
    # the child is actually listening and the proxy chain works end-to-end.
    cookie = _login(port, "testpw")
    deadline = time.time() + 15
    while time.time() < deadline:
        st, _ = _req("GET", port, "/", headers={"Cookie": cookie})
        if st == 200:
            break
        time.sleep(0.3)
    else:
        pytest.fail("opencode child did not become proxy-ready in time")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def unconfigured_manager(tmp_path, fake_bin):
    port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    proc = _start_manager(port, str(data), fake_bin)
    if not _wait_ready(port):
        proc.kill()
        out = proc.communicate(timeout=5)[0] or ""
        pytest.fail(f"manager did not start: {out}")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def _login(port, password, nxt="/manage"):
    st, hdr, _ = _req("POST", port, "/manage/login", body=f"password={password}&next={nxt}", raw=True)
    cookie = hdr.get("Set-Cookie", "")
    return cookie.split(";")[0]


# ─── unconfigured (first run) ──────────────────────────────────────────────────


def test_unconfigured_root_serves_form(unconfigured_manager):
    port = unconfigured_manager
    st, body = _req("GET", port, "/")
    assert st == 200
    assert "first-run configuration" in body or "opencode" in body


def test_unconfigured_health_healthy(unconfigured_manager):
    st, body = _req("GET", unconfigured_manager, "/health")
    assert st == 200
    assert json.loads(body)["healthy"] is True


def test_unconfigured_manage_redirects_to_setup(unconfigured_manager):
    st, hdr, _ = _req("GET", unconfigured_manager, "/manage", raw=True)
    assert st == 302
    assert hdr["Location"].endswith("/setup")


def test_unconfigured_nonsetup_404(unconfigured_manager):
    st, _ = _req("GET", unconfigured_manager, "/session")
    assert st == 404


# ─── configured: auth gate ──────────────────────────────────────────────────────


def test_configured_root_without_cookie_redirects_to_login(configured_manager):
    st, hdr, _ = _req("GET", configured_manager, "/", raw=True)
    assert st == 302
    assert "/manage/login" in hdr["Location"]


def test_configured_event_without_cookie_redirects(configured_manager):
    st, _ = _req("GET", configured_manager, "/event")
    assert st == 302


def test_configured_tampered_cookie_rejected(configured_manager):
    st, _ = _req("GET", configured_manager, "/", headers={"Cookie": "oc_session=999.bogus"})
    assert st == 302


# ─── configured: login + proxy + manage ─────────────────────────────────────────


def test_login_wrong_password_renders_error(configured_manager):
    st, body = _req("POST", configured_manager, "/manage/login", body="password=wrong")
    assert st == 200
    assert "Incorrect password" in body


def test_login_correct_sets_cookie_and_redirects(configured_manager):
    cookie = _login(configured_manager, "testpw", nxt="/manage")
    assert cookie.startswith("oc_session=")


def test_proxy_with_cookie_serves_child(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/", headers={"Cookie": cookie})
    assert st == 200
    assert body == "FAKE UI /"  # auth injected by manager; child saw no basic-auth challenge


def test_proxy_post_forwarded(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("POST", configured_manager, "/session", headers={"Cookie": cookie}, body="msg=hi")
    assert st == 200
    assert body == "FAKE POST /session msg=hi"


def test_proxy_sse_streams(configured_manager):
    cookie = _login(configured_manager, "testpw")
    c = http.client.HTTPConnection("127.0.0.1", configured_manager, timeout=5)
    c.request("GET", "/event", headers={"Cookie": cookie})
    r = c.getresponse()
    assert r.status == 200
    assert r.getheader("Content-Type") == "text/event-stream"
    buf = b""
    for _ in range(2):
        buf += r.read(4096)
        if b"hello-" in buf:
            break
    c.close()
    assert b"data: hello-0" in buf


def test_manage_dashboard_with_cookie(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/manage", headers={"Cookie": cookie})
    assert st == 200
    assert "dashboard" in body
    assert "Reconfigure" in body


def test_manage_logs_json(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/manage/logs", headers={"Cookie": cookie})
    assert st == 200
    assert isinstance(json.loads(body)["lines"], list)


def test_manage_restart(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, hdr, _ = _req("POST", configured_manager, "/manage/restart", headers={"Cookie": cookie}, raw=True)
    assert st == 302
    # child comes back up — wait for proxy to serve (not just /health)
    deadline = time.time() + 15
    while time.time() < deadline:
        st, _ = _req("GET", configured_manager, "/", headers={"Cookie": cookie})
        if st == 200:
            return
        time.sleep(0.3)
    pytest.fail("child did not come back up after restart")


def test_setup_with_cookie_renders_reconfigure_form(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/setup", headers={"Cookie": cookie})
    assert st == 200


def test_unauth_setup_redirect_encodes_next(configured_manager):
    st, hdr, _ = _req("GET", configured_manager, "/setup", raw=True)
    assert st == 302
    assert hdr["Location"] == "/manage/login?next=%2Fsetup"


def test_login_with_next_setup_lands_on_form(configured_manager):
    cookie = _login(configured_manager, "testpw", nxt="/setup")
    st, body = _req("GET", configured_manager, "/setup", headers={"Cookie": cookie})
    assert st == 200
    assert 'action="/setup"' in body
    assert "LLM Provider" in body


def test_login_wrong_password_preserves_next_setup(configured_manager):
    st, body = _req("POST", configured_manager, "/manage/login", body="password=wrong&next=/setup")
    assert st == 200
    assert "Incorrect password" in body
    assert 'name="next" value="/setup"' in body


def test_authenticated_login_redirects_to_next(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, hdr, _ = _req("GET", configured_manager, "/manage/login?next=/setup", headers={"Cookie": cookie}, raw=True)
    assert st == 302
    assert hdr["Location"] == "/setup"


def test_unauth_post_setup_redirects_to_login(configured_manager):
    st, hdr, _ = _req("POST", configured_manager, "/setup", body="provider=anthropic&apikey=x", raw=True)
    assert st == 302
    assert hdr["Location"] == "/manage/login?next=%2Fsetup"


def test_setup_trailing_slash_serves_form(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/setup/", headers={"Cookie": cookie})
    assert st == 200
    assert "LLM Provider" in body


# ─── first-run save flow (auto-login + child start) ─────────────────────────────


def test_first_run_save_starts_child_and_auto_logs_in(tmp_path, fake_bin):
    port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    proc = _start_manager(port, str(data), fake_bin)
    try:
        assert _wait_ready(port)
        # custom provider avoids needing a models.dev fetch
        form = (
            "provider=custom&envvar=CUSTOM_API_KEY&baseurl=https://gw.example.com/v1"
            "&apikey=fakekey&model=m1&password=newpw&gitname=oc&gitemail=oc@x"
        )
        st, hdr, body = _req("POST", port, "/setup", body=form, raw=True)
        assert st == 200
        # auto-login cookie issued
        set_cookie = hdr.get("Set-Cookie", "")
        assert "oc_session=" in set_cookie
        cookie = set_cookie.split(";")[0]
        # .setup.env persisted
        env_text = (data / ".setup.env").read_text()
        assert "OPENCODE_SERVER_PASSWORD=newpw" in env_text
        assert "CUSTOM_API_KEY=fakekey" in env_text
        # child comes up — wait for the proxy to actually serve (not just /health,
        # which reports healthy during the startup window too)
        deadline = time.time() + 15
        while time.time() < deadline:
            st, b = _req("GET", port, "/", headers={"Cookie": cookie})
            if st == 200:
                break
            time.sleep(0.25)
        else:
            pytest.fail("child did not come up after setup save")
        # auto-login cookie proxies to the child
        assert st == 200
        assert b == "FAKE UI /"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


# ─── pure helpers ───────────────────────────────────────────────────────────────


def test_session_cookie_roundtrip():
    secret = b"k" * 32
    val = wizard.make_session_cookie(secret)
    assert wizard.verify_session_cookie(secret, val) is True


def test_session_cookie_rejects_tamper_and_expiry():
    secret = b"k" * 32
    val = wizard.make_session_cookie(secret)
    exp, sig = val.split(".", 1)
    assert wizard.verify_session_cookie(secret, f"{exp}.deadbeef") is False
    assert wizard.verify_session_cookie(secret, "0.0") is False
    assert wizard.verify_session_cookie(secret, "") is False


# ─── Settings (unit) ───────────────────────────────────────────────────────────


def test_settings_update_preserves_other_keys(tmp_path):
    s = wizard.Settings(str(tmp_path))
    s.write({"OPENCODE_SERVER_PASSWORD": "pw", "ANTHROPIC_API_KEY": "k1", "OPENCODE_PROVIDER": "anthropic"})
    s.update({"ANTHROPIC_API_KEY": "k2"})
    out = s.load()
    assert out["ANTHROPIC_API_KEY"] == "k2"
    assert out["OPENCODE_SERVER_PASSWORD"] == "pw"
    assert out["OPENCODE_PROVIDER"] == "anthropic"


def test_settings_write_is_chmod_600(tmp_path):
    s = wizard.Settings(str(tmp_path))
    s.write({"OPENCODE_SERVER_PASSWORD": "pw"})
    mode = oct(os.stat(os.path.join(str(tmp_path), ".setup.env")).st_mode & 0o777)
    assert mode == "0o600"


def test_settings_load_missing_returns_empty(tmp_path):
    s = wizard.Settings(str(tmp_path))
    assert s.load() == {}


# ─── management endpoints (integration) ────────────────────────────────────────


def test_manage_status_json(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/manage/status", headers={"Cookie": cookie})
    assert st == 200
    j = json.loads(body)
    assert j["configured"] is True
    assert j["provider"] == "anthropic"
    assert "child_up" in j


def test_manage_dashboard_shows_key_section(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("GET", configured_manager, "/manage", headers={"Cookie": cookie})
    assert st == 200
    assert "Provider key" in body
    assert "ANTHROPIC_API_KEY" in body
    assert "Revalidate key" in body


def test_manage_keys_rotate_updates_setup_env(tmp_path, fake_bin):
    port = _free_port()
    data = tmp_path / "data"
    data.mkdir()
    (data / ".setup.env").write_text(
        "OPENCODE_SERVER_PASSWORD=testpw\nANTHROPIC_API_KEY=oldkey\nOPENCODE_PROVIDER=anthropic\nOPENCODE_PROVIDER_KEY_ENV=ANTHROPIC_API_KEY\n"
    )
    os.chmod(data / ".setup.env", 0o600)
    proc = _start_manager(port, str(data), fake_bin)
    try:
        assert _wait_ready(port)
        cookie = _login(port, "testpw")
        st, hdr, _ = _req(
            "POST",
            port,
            "/manage/keys/rotate",
            headers={"Cookie": cookie},
            body="envvar=ANTHROPIC_API_KEY&apikey=rotatedkey",
            raw=True,
        )
        assert st == 302
        # .setup.env updated with the new key, password preserved
        env = (data / ".setup.env").read_text()
        assert "ANTHROPIC_API_KEY=rotatedkey" in env
        assert "OPENCODE_SERVER_PASSWORD=testpw" in env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_manage_revalidate_returns_shape(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req("POST", configured_manager, "/manage/revalidate", headers={"Cookie": cookie})
    assert st == 200
    j = json.loads(body)
    assert "ok" in j and "detail" in j


def test_manage_keys_rotate_rejects_bad_envvar(configured_manager):
    cookie = _login(configured_manager, "testpw")
    st, body = _req(
        "POST",
        configured_manager,
        "/manage/keys/rotate",
        headers={"Cookie": cookie},
        body="envvar=1bad&apikey=x",
    )
    assert st == 400
