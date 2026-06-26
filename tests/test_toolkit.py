import json
import os
import subprocess
import sys

import generate_config as gc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLKIT = os.path.join(ROOT, "mcps", "toolkit.py")

EXPECTED_TOOLS = {
    "calculate",
    "datetime",
    "text",
    "generate_id",
    "convert_units",
    "semver",
    "network",
    "color",
}


# ─── generate_config: bundled toolkit preset ───────────────────────────────────


def test_toolkit_on_by_default(monkeypatch):
    monkeypatch.delenv("ENABLED_MCPS", raising=False)
    monkeypatch.delenv("DISABLE_TOOLKIT_MCP", raising=False)
    out = gc.build_mcp()
    assert "toolkit" in out
    assert out["toolkit"]["type"] == "local"
    assert out["toolkit"]["command"] == ["python3", "/mcps/toolkit.py"]
    assert out["toolkit"]["enabled"] is True


def test_toolkit_disabled_by_env(monkeypatch):
    monkeypatch.delenv("ENABLED_MCPS", raising=False)
    monkeypatch.setenv("DISABLE_TOOLKIT_MCP", "1")
    out = gc.build_mcp()
    assert "toolkit" not in out


def test_toolkit_coexists_with_opt_in(monkeypatch):
    monkeypatch.setenv("ENABLED_MCPS", "context7")
    monkeypatch.delenv("DISABLE_TOOLKIT_MCP", raising=False)
    out = gc.build_mcp()
    assert set(out) == {"context7", "toolkit"}


def test_toolkit_block_matches_mcp_local_schema(monkeypatch):
    monkeypatch.delenv("ENABLED_MCPS", raising=False)
    monkeypatch.delenv("DISABLE_TOOLKIT_MCP", raising=False)
    t = gc.build_mcp()["toolkit"]
    allowed = {"type", "command", "environment", "cwd", "enabled", "timeout"}
    assert set(t) <= allowed  # no extra keys (schema has additionalProperties: false)


# ─── toolkit.py: MCP stdio contract ────────────────────────────────────────────


def _call(proc, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("id") == msg_id:
            return obj


def _spawn():
    return subprocess.Popen(
        [sys.executable, TOOLKIT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, OC_MCP_DEBUG=""),
    )


def test_toolkit_handshake_and_tools_list():
    proc = _spawn()
    try:
        init = _call(
            proc,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
            1,
        )
        assert init["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in init["result"]["capabilities"]
        tools = _call(proc, "tools/list", None, 2)["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == EXPECTED_TOOLS
        assert all("inputSchema" in t for t in tools)
    finally:
        proc.stdin.close()
        proc.wait(timeout=3)


def test_toolkit_calculate_call():
    proc = _spawn()
    try:
        _call(proc, "initialize", {}, 1)
        resp = _call(proc, "tools/call", {"name": "calculate", "arguments": {"expression": "2^10"}}, 2)
        result = resp["result"]
        assert result.get("isError") is not True
        body = json.loads(result["content"][0]["text"])
        assert body["result"] == "1024"
    finally:
        proc.stdin.close()
        proc.wait(timeout=3)


def test_toolkit_error_is_flagged():
    proc = _spawn()
    try:
        _call(proc, "initialize", {}, 1)
        resp = _call(proc, "tools/call", {"name": "calculate", "arguments": {"expression": "__import__('os')"}}, 2)
        assert resp["result"]["isError"] is True
    finally:
        proc.stdin.close()
        proc.wait(timeout=3)
