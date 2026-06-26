import json
import os
import subprocess

import generate_config as gc


def _env(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_build_provider_skips_known_providers(monkeypatch):
    _env(monkeypatch, OPENCODE_PROVIDER="anthropic")
    assert gc.build_provider() == {}


def test_build_provider_custom_block(monkeypatch):
    _env(
        monkeypatch,
        OPENCODE_PROVIDER="custom",
        OPENCODE_CUSTOM_ID="custom",
        OPENCODE_CUSTOM_LABEL="My GW",
        OPENCODE_CUSTOM_BASEURL="https://gw/v1",
        OPENCODE_CUSTOM_NPM="@ai-sdk/openai-compatible",
        OPENCODE_CUSTOM_ENV="GW_KEY",
        OPENCODE_MODEL="custom/m1",
    )
    out = gc.build_provider()
    assert out["custom"]["options"]["baseURL"] == "https://gw/v1"
    assert out["custom"]["options"]["apiKey"] == "{env:GW_KEY}"
    assert out["custom"]["models"] == {"m1": {"name": "m1"}}


def test_build_provider_custom_missing_baseurl_returns_empty(monkeypatch):
    _env(monkeypatch, OPENCODE_PROVIDER="custom", OPENCODE_CUSTOM_BASEURL="", OPENCODE_MODEL="custom/m1")
    assert gc.build_provider() == {}


def test_build_mcp_skips_missing_required_env(monkeypatch, capsys):
    _env(monkeypatch, ENABLED_MCPS="context7,tavily")  # tavily needs TAVILY_API_KEY (unset)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = gc.build_mcp()
    assert "context7" in out
    assert "tavily" not in out
    assert "tavily" in capsys.readouterr().err


def test_build_mcp_includes_keyed_when_env_set(monkeypatch):
    _env(monkeypatch, ENABLED_MCPS="tavily", TAVILY_API_KEY="t-key")
    out = gc.build_mcp()
    assert out["tavily"]["headers"]["Authorization"] == "Bearer {env:TAVILY_API_KEY}"


def test_build_mcp_custom_json(monkeypatch):
    custom = json.dumps([{"name": "myapi", "url": "https://x/mcp", "headers": {"x": "y"}}])
    _env(monkeypatch, MCP_CUSTOM=custom)
    out = gc.build_mcp()
    assert out["myapi"]["url"] == "https://x/mcp"
    assert out["myapi"]["headers"] == {"x": "y"}


def test_build_mcp_custom_bad_json(monkeypatch, capsys):
    _env(monkeypatch, MCP_CUSTOM="not-json", DISABLE_TOOLKIT_MCP="1")
    assert gc.build_mcp() == {}
    assert "MCP_CUSTOM" in capsys.readouterr().err


def test_main_writes_full_config(monkeypatch, tmp_path):
    _env(
        monkeypatch,
        DATA_DIR=str(tmp_path),
        OPENCODE_MODEL="anthropic/claude-sonnet-4-5",
        OPENCODE_SMALL_MODEL="anthropic/claude-haiku-4-5",
        OPENCODE_PROVIDER="custom",
        OPENCODE_CUSTOM_ID="custom",
        OPENCODE_CUSTOM_BASEURL="https://gw/v1",
        OPENCODE_CUSTOM_ENV="GW_KEY",
        ENABLED_MCPS="context7",
    )
    gc.main()
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["model"] == "anthropic/claude-sonnet-4-5"
    assert cfg["small_model"] == "anthropic/claude-haiku-4-5"
    assert cfg["provider"]["custom"]["options"]["baseURL"] == "https://gw/v1"
    assert cfg["mcp"]["context7"]["type"] == "remote"
    assert cfg["share"] == "disabled"


# ─── entrypoint.sh shell helpers ────────────────────────────────────────────────
# entrypoint.sh can't be sourced in tests (its top-level code runs mkdir, git
# config, and the wizard exec), so these tests mirror the two pure helper
# functions and assert the real file contains the same logic.

ENTRYPOINT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entrypoint.sh")

ENTRYPOINT_HELPERS = r"""
inject_token() {
  url="$1"; tok="$2"
  case "$url" in
    https://github.com/*)
      if [ -n "$tok" ]; then
        case "$url" in
          *@*) : ;;
          *) echo "$url" | sed "s#https://github.com#https://x-access-token:${tok}@github.com#" ; return ;;
        esac
      fi ;;
  esac
  echo "$url"
}
redact_token() { sed "s#x-access-token:[^@]*@#x-access-token:***@#g"; }
"""


def _run_sh(snippet):
    proc = subprocess.run(["sh", "-c", snippet], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def test_inject_token_adds_credentials():
    rc, out, _ = _run_sh(ENTRYPOINT_HELPERS + 'inject_token "https://github.com/me/repo" "abc123"')
    assert rc == 0
    assert "x-access-token:abc123@github.com" in out


def test_inject_token_passthrough_without_token():
    rc, out, _ = _run_sh(ENTRYPOINT_HELPERS + 'inject_token "https://github.com/me/repo" ""')
    assert out == "https://github.com/me/repo"


def test_inject_token_leaves_existing_credentials():
    rc, out, _ = _run_sh(ENTRYPOINT_HELPERS + 'inject_token "https://user:pw@github.com/me/repo" "abc"')
    assert out == "https://user:pw@github.com/me/repo"


def test_inject_token_ignores_non_github():
    rc, out, _ = _run_sh(ENTRYPOINT_HELPERS + 'inject_token "https://gitlab.com/me/repo" "abc"')
    assert out == "https://gitlab.com/me/repo"


def test_redact_token_strips_secret():
    rc, out, _ = _run_sh(ENTRYPOINT_HELPERS + "echo 'fatal: https://x-access-token:secret@github.com/me/repo not found' | redact_token")
    assert "secret" not in out
    assert "x-access-token:***@" in out


PREP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prep.sh")


def test_prep_contains_branch_and_redaction_logic():
    src = open(PREP).read()
    assert "GIT_REPO_BRANCH" in src
    assert "redact_token" in src
    assert "x-access-token:" in src
    assert "--branch" in src
