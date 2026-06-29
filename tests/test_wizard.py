import io
import json
import urllib.error

import wizard

# ─── _auto_default ─────────────────────────────────────────────────────────────


def test_auto_default_prefers_tool_call_and_recency():
    models = {
        "old-tools": {"last_updated": "2025-01-01", "tool_call": True},
        "new-notools": {"last_updated": "2026-01-01", "tool_call": False},
        "new-tools": {"last_updated": "2026-02-01", "tool_call": True},
    }
    assert wizard._auto_default(models) == "new-tools"


def test_auto_default_falls_back_to_most_recent_without_tool_call():
    models = {
        "a": {"last_updated": "2025-01-01", "tool_call": False},
        "b": {"last_updated": "2026-01-01", "tool_call": False},
    }
    assert wizard._auto_default(models) == "b"


def test_auto_default_empty():
    assert wizard._auto_default({}) == ""


# ─── _normalize ────────────────────────────────────────────────────────────────


def _raw():
    return {
        "anthropic": {
            "id": "anthropic",
            "name": "Anthropic",
            "env": ["ANTHROPIC_API_KEY"],
            "npm": "@ai-sdk/anthropic",
            "api": None,
            "models": {
                "claude-x": {
                    "name": "Claude X",
                    "tool_call": True,
                    "last_updated": "2026-01-01",
                    "limit": {"context": 200000},
                    "cost": {"input": 3, "output": 15},
                },
            },
        },
        "deepseek": {
            "id": "deepseek",
            "name": "DeepSeek",
            "env": ["DEEPSEEK_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.deepseek.com",
            "models": {"chat": {"name": "Chat", "tool_call": False, "last_updated": "2025-01-01"}},
        },
        "cloudflare-workers-ai": {
            "id": "cloudflare-workers-ai",
            "name": "CF",
            "env": ["CF_API_KEY"],
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.cloudflare.com/${CF_ID}/v1",
            "models": {},
        },
    }


def test_normalize_maps_fields_and_marks_placeholder():
    out = wizard._normalize(_raw())
    assert out["anthropic"]["label"] == "Anthropic"
    assert out["anthropic"]["env_var"] == "ANTHROPIC_API_KEY"
    assert out["anthropic"]["npm"] == "@ai-sdk/anthropic"
    assert out["anthropic"]["placeholder"] is False
    assert out["deepseek"]["placeholder"] is False
    assert "cloudflare-workers-ai" not in out  # placeholder base URL -> dumped
    assert out["anthropic"]["models"]["claude-x"]["context"] == 200000
    assert out["anthropic"]["models"]["claude-x"]["tool_call"] is True


def test_normalize_uses_curated_default_when_present():
    out = wizard._normalize(_raw())
    assert out["deepseek"]["default_model"] == wizard.CURATED_DEFAULTS["deepseek"]


def test_normalize_dumps_local_and_oauth_only_providers():
    raw = _raw()
    raw["lmstudio"] = {
        "id": "lmstudio",
        "name": "LM Studio",
        "env": ["LMSTUDIO_API_KEY"],
        "npm": "@ai-sdk/openai-compatible",
        "api": "http://127.0.0.1:1234/v1",
        "models": {},
    }
    raw["github-copilot"] = {
        "id": "github-copilot",
        "name": "GitHub Copilot",
        "env": ["GITHUB_TOKEN"],
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://api.githubcopilot.com",
        "models": {},
    }
    out = wizard._normalize(raw)
    assert "lmstudio" not in out  # localhost endpoint
    assert "github-copilot" not in out  # OAuth-only (SKIP_PROVIDERS)
    assert "cloudflare-workers-ai" not in out  # placeholder base URL
    assert "anthropic" in out  # kept


# ─── get_providers (offline via cache) ──────────────────────────────────────────


def test_get_providers_reads_cache_without_network(tmp_path):
    cache = tmp_path / wizard.CACHE_NAME
    cache.write_text(json.dumps(_raw()))
    # bust the in-memory cache so this data_dir is loaded fresh
    wizard._PROV_CACHE.pop(str(tmp_path), None)
    prov = wizard.get_providers(str(tmp_path))
    assert "anthropic" in prov
    assert prov["deepseek"]["env_var"] == "DEEPSEEK_API_KEY"


# ─── auth + live models url ─────────────────────────────────────────────────────


def test_auth_headers_anthropic_uses_x_api_key():
    cfg = wizard._normalize(_raw())["anthropic"]
    h = wizard._auth_headers("anthropic", cfg, "sk-abc")
    assert h["x-api-key"] == "sk-abc"
    assert h["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in h


def test_auth_headers_bearer_for_openai_compatible():
    cfg = wizard._normalize(_raw())["deepseek"]
    h = wizard._auth_headers("deepseek", cfg, "sk-abc")
    assert h["Authorization"] == "Bearer sk-abc"


def test_live_models_url_native_uses_map():
    cfg = wizard._normalize(_raw())["anthropic"]
    assert wizard._live_models_url("anthropic", cfg) == wizard.LIVE_MODELS_URL["anthropic"]


def test_live_models_url_openai_compatible_derives_from_api():
    cfg = wizard._normalize(_raw())["deepseek"]
    assert wizard._live_models_url("deepseek", cfg) == "https://api.deepseek.com/models"


def test_live_models_url_placeholder_returns_none():
    # placeholder providers are dumped by _normalize, so build a synthetic cfg
    # to exercise the defense-in-depth guard in _live_models_url.
    cfg = {"api": "https://x/${ID}/v1", "placeholder": True, "npm": "@ai-sdk/openai-compatible"}
    assert wizard._live_models_url("synthetic", cfg) is None


# ─── validate_provider_key (mocked urlopen) ─────────────────────────────────────


class FakeResp:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_resp(data=None):
    return FakeResp(json.dumps({"data": data or [{"id": "m1"}, {"id": "m2"}]}).encode())


def test_validate_provider_key_ok(monkeypatch):
    monkeypatch.setattr(wizard.urllib.request, "urlopen", lambda *a, **k: _ok_resp())
    prov = wizard._normalize(_raw())
    ok, msg = wizard.validate_provider_key("deepseek", "k", prov)
    assert ok is True
    assert "2 models" in msg


def test_validate_provider_key_401(monkeypatch):
    def raise_401(*a, **k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(wizard.urllib.request, "urlopen", raise_401)
    prov = wizard._normalize(_raw())
    ok, msg = wizard.validate_provider_key("deepseek", "bad", prov)
    assert ok is False
    assert "401" in msg


def test_validate_provider_key_placeholder_no_live_check(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not call network for placeholder provider")

    monkeypatch.setattr(wizard.urllib.request, "urlopen", boom)
    # placeholder providers are dumped by _normalize; pass a synthetic entry
    # to exercise the defense-in-depth guard in validate_provider_key.
    prov = {"synthetic": {"api": "https://x/${ID}/v1", "placeholder": True, "npm": "@ai-sdk/openai-compatible", "env_var": "X"}}
    ok, msg = wizard.validate_provider_key("synthetic", "k", prov)
    assert ok is True
    assert called["n"] == 0


def test_validate_provider_key_custom_uses_baseurl(monkeypatch):
    captured = {}

    def fake(req, *a, **k):
        captured["url"] = req.full_url
        return _ok_resp()

    monkeypatch.setattr(wizard.urllib.request, "urlopen", fake)
    ok, msg = wizard.validate_provider_key("custom", "k", {}, custom={"baseurl": "https://gw.example.com/v1", "env": "GW_KEY"})
    assert ok is True
    assert captured["url"] == "https://gw.example.com/v1/models"


def test_validate_provider_key_custom_missing_baseurl():
    ok, msg = wizard.validate_provider_key("custom", "k", {}, custom={"baseurl": ""})
    assert ok is False
    assert "base URL" in msg


# ─── validate_github_token (mocked) ─────────────────────────────────────────────


def test_validate_github_token_ok_with_scopes(monkeypatch):
    def fake(req, *a, **k):
        return FakeResp(json.dumps({"login": "alice"}).encode(), headers={"X-OAuth-Scopes": "repo, read:user"})

    monkeypatch.setattr(wizard.urllib.request, "urlopen", fake)
    ok, user, scopes = wizard.validate_github_token("tok")
    assert ok is True
    assert user == "alice"
    assert "repo" in scopes


def test_validate_github_token_empty():
    ok, user, scopes = wizard.validate_github_token("")
    assert ok is False


# ─── load_existing ──────────────────────────────────────────────────────────────


def test_load_existing_strips_quotes(tmp_path):
    env = tmp_path / ".setup.env"
    env.write_text(
        "# comment\nOPENCODE_PROVIDER='anthropic'\nOPENCODE_MODEL=\"anthropic/claude-sonnet-4-5\"\nPW=bare value with spaces\nEMPTY=\n"
    )
    out = wizard.load_existing(str(tmp_path))
    assert out["OPENCODE_PROVIDER"] == "anthropic"
    assert out["OPENCODE_MODEL"] == "anthropic/claude-sonnet-4-5"
    assert out["PW"] == "bare value with spaces"
    assert out["EMPTY"] == ""


def test_load_existing_missing_file(tmp_path):
    assert wizard.load_existing(str(tmp_path)) == {}


# ─── volume_mounted ─────────────────────────────────────────────────────────────


def test_volume_mounted_detects_dedicated_mount(monkeypatch):
    import builtins

    real_open = builtins.open

    class FakeFile:
        def __init__(self, text):
            self.text = text

        def __enter__(self):
            return self.text.splitlines()

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(self.text.splitlines())

    def fake_open(path, *a, **k):
        if str(path) == "/proc/mounts":
            return FakeFile("overlay / overlay rw 0 0\n/dev/vdb1 /data ext4 rw 0 0\n")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert wizard.volume_mounted("/data") is True
    assert wizard.volume_mounted("/var") is False


def test_volume_mounted_none_when_no_proc(monkeypatch):
    import builtins

    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/mounts":
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert wizard.volume_mounted("/data") is None


# ─── _rate_limited ──────────────────────────────────────────────────────────────


def test_rate_limited_allows_then_blocks(monkeypatch):
    wizard._RATE.clear()
    t = [0.0]
    monkeypatch.setattr(wizard.time, "time", lambda: t[0])
    allowed = 0
    for _ in range(wizard._RATE_MAX_POST):
        t[0] += 0.001
        if not wizard._rate_limited("1.2.3.4"):
            allowed += 1
    assert allowed == wizard._RATE_MAX_POST
    assert wizard._rate_limited("1.2.3.4") is True
    # different ip has its own budget
    assert wizard._rate_limited("5.6.7.8") is False
    wizard._RATE.clear()


# ─── _ENV_VAR_RE ───────────────────────────────────────────────────────────────


def test_env_var_re_accepts_valid_identifiers():
    assert wizard._ENV_VAR_RE.match("CUSTOM_API_KEY")
    assert wizard._ENV_VAR_RE.match("ANTHROPIC_API_KEY")
    assert wizard._ENV_VAR_RE.match("_FOO")
    assert wizard._ENV_VAR_RE.match("A1")


def test_env_var_re_rejects_invalid():
    assert not wizard._ENV_VAR_RE.match("1FOO")  # leading digit
    assert not wizard._ENV_VAR_RE.match("FOO-BAR")  # hyphen
    assert not wizard._ENV_VAR_RE.match("")  # empty
    assert not wizard._ENV_VAR_RE.match("FOO BAR")  # space
    assert not wizard._ENV_VAR_RE.match("FOO.BAR")  # dot
