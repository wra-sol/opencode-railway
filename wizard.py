#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""First-run setup wizard for the opencode Railway template.

Served on $PORT by entrypoint.sh when OPENCODE_SERVER_PASSWORD is unset. The
provider/model catalog is sourced from models.dev (the same artifact opencode
itself uses), cached on the persistent volume with a vendored build-time
snapshot fallback. Collects: LLM provider + key (validated live), model +
small model (populated from models.dev), repo URL + branch, GitHub PAT
(validated against the GitHub API, scope-checked), server password, and git
identity. Persists to /data/.setup.env, then execs back into /entrypoint.sh
so opencode web comes up without a restart.

The opencode runtime config (/data/opencode.json) is written by
generate_config.py — the single source of truth — which entrypoint.sh runs on
every boot; the wizard only owns /data/.setup.env. Stdlib only.
"""

import argparse
import base64
import fcntl
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import shlex
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

# ─── Constants ─────────────────────────────────────────────────────────────────

UA = "opencode-railway-wizard/2.0"
_SSL = ssl.create_default_context()

MODELS_URL = "https://models.dev/api.json"
SNAPSHOT_PATH = "/wizard/models.dev.snapshot.json"
CACHE_NAME = "models.dev.api.json"
CACHE_TTL = 24 * 3600

MAX_BODY = 64 * 1024

# ─── Rate limiting ─────────────────────────────────────────────────────────────
# Per-IP rate limiting with login lockout. The primary key is the socket peer
# address (Railway's proxy), not X-Forwarded-For — which can be spoofed. If
# TRUSTED_XFF_HOPS is set (default 1 for Railway's single proxy hop), the
# Nth-from-right XFF value is used as the "real client" key, ignoring any
# spoofed values to its left.

_RATE = {}
_RATE_WINDOW = 60
_RATE_MAX_POST = 30  # state-changing + management POSTs
_RATE_MAX_LOGIN = 10  # login attempts
_RATE_MAX_GET = 120  # GET routes (dashboard, logs, proxy)
_LOCKOUT = {}  # ip -> [locked_until, consecutive_failures]
_LOCKOUT_THRESHOLD = 5  # failures before lockout kicks in
_LOCKOUT_STEPS = [60, 300, 900]  # exponential: 1min, 5min, 15min
_TRUSTED_XFF_HOPS = int(os.environ.get("TRUSTED_XFF_HOPS", "1"))

# Valid POSIX shell identifier for custom provider key env var name.
_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# ─── Manager (reverse proxy + child supervisor) ────────────────────────────────
# Hop-by-hop headers (RFC 7230 §6.1) stripped when proxying. transfer-encoding is
# stripped from responses because http.client decodes chunked bodies for us.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# Request headers we strip and re-send ourselves when proxying to the child.
HOP_BY_HOP_REQ = HOP_BY_HOP | {"host", "authorization", "content-length"}
SESSION_COOKIE = "oc_session"
SESSION_TTL = 7 * 24 * 3600
LOG_RING = 400  # max lines kept for /manage/logs
CHILD_RESPAWN_MAX = 5

# Curated "popular" providers shown first in the picker (display order).
CURATED = [
    "anthropic",
    "openai",
    "openrouter",
    "opencode",
    "deepseek",
    "groq",
    "xai",
    "togetherai",
    "fireworks-ai",
    "cerebras",
    "mistral",
    "moonshotai",
    "nvidia",
]

# Default model per curated provider (verified against models.dev).
CURATED_DEFAULTS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-5.2",
    "openrouter": "anthropic/claude-sonnet-4.5",
    "opencode": "glm-4.7",
    "deepseek": "deepseek-chat",
    "groq": "llama-3.3-70b-versatile",
    "xai": "grok-4.3",
    "togetherai": "deepseek-ai/DeepSeek-V4-Pro",
    "fireworks-ai": "accounts/fireworks/routers/kimi-k2p7-code-fast",
    "cerebras": "gpt-oss-120b",
    "mistral": "codestral-latest",
    "moonshotai": "kimi-k2.7-code",
    "nvidia": "moonshotai/kimi-k2.6",
}

# Extra auth headers beyond the key (only anthropic needs a version header).
EXTRA_HEADERS = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}

# Live /models endpoints for native-SDK providers (models.dev has no api URL
# for these). Used only to validate the key live; model lists come from
# models.dev. OpenAI-compatible providers derive this as api + "/models".
LIVE_MODELS_URL = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "xai": "https://api.x.ai/v1/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "togetherai": "https://api.together.xyz/v1/models",
    "cerebras": "https://api.cerebras.ai/v1/models",
}

# Providers to exclude from the wizard even though they're in models.dev:
#  - local-only endpoints (127.0.0.1/localhost) that can't be reached from Railway
#  - OAuth/device-code flows that can't be configured with a pasted API key headlessly
SKIP_PROVIDERS = {"atomic-chat", "lmstudio", "privatemode-ai", "github-copilot"}


def _client_ip(handler):
    """Real client IP for rate limiting.

    Primary key is the socket peer address (Railway's proxy). If
    TRUSTED_XFF_HOPS > 0, take the Nth-from-right value from X-Forwarded-For
    as the real client — only N hops are trusted, spoofed values further
    left are discarded. Default 1 = trust the last hop (Railway → us).
    """
    if _TRUSTED_XFF_HOPS > 0:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) >= _TRUSTED_XFF_HOPS:
                # Nth-from-right: e.g. N=1 → parts[-1], N=2 → parts[-2]
                return parts[-_TRUSTED_XFF_HOPS]
    return handler.client_address[0]


def _rate_limited(ip, max_reqs=_RATE_MAX_POST, bucket="post"):
    """Generic per-IP rate limit. Returns True if the IP has exceeded max_reqs
    in the current window. GET and POST use separate buckets so heavy GET
    traffic (e.g. polling /manage/logs) doesn't exhaust the POST budget."""
    key = f"{ip}:{bucket}"
    now = time.time()
    rec = _RATE.get(key)
    if not rec or now - rec[0] > _RATE_WINDOW:
        _RATE[key] = [now, 1]
        return False
    rec[1] += 1
    return rec[1] > max_reqs


def _login_locked(ip):
    """True if the IP is currently locked out due to repeated login failures."""
    rec = _LOCKOUT.get(ip)
    if not rec:
        return False
    locked_until, _failures = rec
    if time.time() < locked_until:
        return True
    return False  # lockout expired; will be reset on next failure or success


def _record_login_failure(ip):
    """Increment consecutive login failures and apply exponential lockout.
    Lockout only kicks in after _LOCKOUT_THRESHOLD failures."""
    rec = _LOCKOUT.get(ip)
    failures = (rec[1] + 1) if rec else 1
    if failures >= _LOCKOUT_THRESHOLD:
        # Lockout duration escalates with failures beyond the threshold
        idx = min(failures - _LOCKOUT_THRESHOLD, len(_LOCKOUT_STEPS) - 1)
        locked_until = time.time() + _LOCKOUT_STEPS[idx]
    else:
        locked_until = 0  # not locked yet
    _LOCKOUT[ip] = [locked_until, failures]


def _record_login_success(ip):
    """Clear lockout state on successful login."""
    _LOCKOUT.pop(ip, None)


# ─── models.dev loader ─────────────────────────────────────────────────────────

_PROV_CACHE = {}


def _http_get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_models_dev(data_dir):
    cache = os.path.join(data_dir, CACHE_NAME)
    fresh = os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < CACHE_TTL
    if fresh:
        try:
            return json.load(open(cache))
        except Exception:
            pass
    try:
        data = _http_get_json(MODELS_URL, timeout=12)
        try:
            with open(cache, "w") as fh:
                fh.write(json.dumps(data))
        except Exception:
            pass
        return data
    except Exception:
        pass
    if os.path.exists(cache):
        try:
            return json.load(open(cache))
        except Exception:
            pass
    if os.path.exists(SNAPSHOT_PATH):
        try:
            return json.load(open(SNAPSHOT_PATH))
        except Exception:
            pass
    return {}


def _auto_default(models):
    if not models:
        return ""
    best = None
    best_tc = None
    for mid, m in models.items():
        lu = m.get("last_updated") or ""
        if m.get("tool_call"):
            if best_tc is None or lu > best_tc[0]:
                best_tc = (lu, mid)
        if best is None or lu > best[0]:
            best = (lu, mid)
    return (best_tc or best)[1]


def _normalize(raw):
    out = {}
    for pid, p in raw.items():
        if pid in SKIP_PROVIDERS:
            continue
        api = p.get("api")
        if api and ("127.0.0.1" in api or "localhost" in api):
            continue
        if api and "${" in api:  # needs extra env vars baked into the URL
            continue
        env = p.get("env") or [None]
        npm = p.get("npm") or ""
        models = {}
        for mid, m in (p.get("models") or {}).items():
            lim = m.get("limit") or {}
            cost = m.get("cost") or {}
            models[mid] = {
                "name": m.get("name") or mid,
                "context": lim.get("context"),
                "tool_call": bool(m.get("tool_call")),
                "reasoning": bool(m.get("reasoning")),
                "cost_in": cost.get("input"),
                "cost_out": cost.get("output"),
                "last_updated": m.get("last_updated") or "",
            }
        out[pid] = {
            "id": pid,
            "label": p.get("name") or pid,
            "env_var": env[0] or "",
            "npm": npm,
            "api": api,
            "placeholder": bool(api) and "${" in api,
            "default_model": CURATED_DEFAULTS.get(pid, _auto_default(models)),
            "extra_headers": EXTRA_HEADERS.get(pid, {}),
            "models": models,
        }
    return out


def get_providers(data_dir):
    if data_dir not in _PROV_CACHE:
        _PROV_CACHE[data_dir] = _normalize(_fetch_models_dev(data_dir))
    return _PROV_CACHE[data_dir]


# ─── Provider key validation ───────────────────────────────────────────────────


def _auth_headers(pid, cfg, api_key):
    h = {"User-Agent": UA}
    if cfg["npm"] == "@ai-sdk/anthropic":
        h["x-api-key"] = api_key
        h["anthropic-version"] = "2023-06-01"
    else:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _live_models_url(pid, cfg):
    if pid in LIVE_MODELS_URL:
        return LIVE_MODELS_URL[pid]
    if cfg.get("api") and not cfg.get("placeholder"):
        return cfg["api"].rstrip("/") + "/models"
    return None


def validate_provider_key(pid, api_key, providers, custom=None):
    """Validate an API key live. Returns (ok, message)."""
    if pid == "custom":
        base = (custom or {}).get("baseurl", "").rstrip("/")
        if not base:
            return False, "Enter a base URL"
        url = base + "/models"
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": UA}
    else:
        cfg = providers.get(pid)
        if not cfg:
            return False, "Unknown provider"
        url = _live_models_url(pid, cfg)
        if not url:
            return True, "Key saved (no live check for this provider)"
        headers = _auth_headers(pid, cfg, api_key)
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=12, context=_SSL) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            n = len(data.get("data", [])) if isinstance(data, dict) else 0
            return True, f"Key valid ({n} models)" if n else "Key valid"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid API key (401)"
        if e.code == 404:
            return True, "Key saved (no /models endpoint to check)"
        return False, f"HTTP {e.code}"
    except urllib.error.URLError:
        return False, "Connection failed"
    except Exception as e:
        return False, str(e)


def validate_github_token(token):
    """Validate a GitHub PAT by calling /user. Returns (ok, username_or_error, scopes)."""
    if not token:
        return False, "No token provided", ""
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            scopes = resp.headers.get("X-OAuth-Scopes", "")
            return True, data.get("login", ""), scopes
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid token", ""
        return False, f"HTTP {e.code}", ""
    except Exception:
        return False, "Connection failed", ""


# ─── MCP catalog ──────────────────────────────────────────────────────────────

MCP_CATALOG = {
    "toolkit": {
        "label": "Toolkit — bundled calculator, dates, text, IDs, units, semver, network, color",
        "type": "local",
        "needs_key": False,
        "bundled": True,
        "hint": "On by default. Pure-Python local tools, no key or network needed. Disable with DISABLE_TOOLKIT_MCP=1.",
    },
    "context7": {
        "label": "Context7 — live library docs",
        "type": "remote",
        "needs_key": False,
        "hint": "Fetches version-specific docs so the agent doesn't hallucinate APIs. No key needed.",
    },
    "gh_grep": {
        "label": "Grep by Vercel — GitHub code search",
        "type": "remote",
        "needs_key": False,
        "hint": "Search code across public GitHub repos. No key needed.",
    },
    "tavily": {
        "label": "Tavily — AI web search + crawl",
        "type": "remote",
        "needs_key": True,
        "key_env": "TAVILY_API_KEY",
        "test_url": "https://mcp.tavily.com/mcp/",
        "key_header": "Authorization",
        "key_value_fmt": "Bearer {key}",
        "hint": "Real-time web search, page extraction, and crawl. Get a key at tavily.com.",
    },
    "exa": {
        "label": "Exa — semantic web search",
        "type": "remote",
        "needs_key": True,
        "key_env": "EXA_API_KEY",
        "test_url": "https://mcp.exa.ai/mcp",
        "key_header": "x-api-key",
        "key_value_fmt": "{key}",
        "hint": "AI-powered semantic search. Get a key at exa.ai.",
    },
    "memory": {
        "label": "Memory — persistent knowledge graph",
        "type": "local",
        "needs_key": False,
        "hint": "Stores entities and relationships across sessions. Runs in-container via npx.",
    },
    "sequential_thinking": {
        "label": "Sequential Thinking — structured reasoning",
        "type": "local",
        "needs_key": False,
        "hint": "Step-by-step reasoning chains for hard problems. Runs in-container via npx.",
    },
    "fetch": {
        "label": "Fetch — web page content extraction",
        "type": "local",
        "needs_key": False,
        "hint": "Fetches web pages and converts to clean markdown. Runs in-container via npx.",
    },
    "brave_search": {
        "label": "Brave Search — web search",
        "type": "local",
        "needs_key": True,
        "key_env": "BRAVE_API_KEY",
        "hint": "Privacy-focused web search. Free tier 2k queries/mo. Key at brave.com/search/api. Runs via npx.",
    },
}

SKILLS = {
    "environment-briefing": "Context for the Railway container — /data layout, commit & push, reconnect, available MCPs/skills.",
    "diagnose": "Disciplined debug loop: reproduce, minimise, hypothesise, instrument, fix, regression-test. Use when something is broken.",
    "git-commit-hygiene": "Clean conventional commit messages and proper staging. Use when preparing a git commit.",
    "pr-review": "Review changes against documented standards and the originating spec. Use when reviewing a branch or PR.",
}


def test_mcp_connection(url, headers):
    """Test a remote MCP server by sending a JSON-RPC initialize request.
    Returns (ok, detail)."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "opencode-wizard", "version": "1.0"}},
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("User-Agent", UA)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=12, context=_SSL) as resp:
            return True, "Connected"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid key (401)"
        if e.code == 403:
            return False, "Forbidden (403)"
        return False, f"HTTP {e.code}"
    except urllib.error.URLError:
        return False, "Connection failed"
    except Exception:
        return False, "Connection failed"


# ─── Existing-config loader ────────────────────────────────────────────────────


def load_existing(data_dir):
    path = os.path.join(data_dir, ".setup.env")
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                v = v[1:-1]
            out[k] = v
    return out


def env_var_already_set(name):
    return bool(os.environ.get(name))


# ─── Volume detection ──────────────────────────────────────────────────────────


def volume_mounted(data_dir):
    """True if data_dir is on a dedicated mount (not the container root fs)."""
    try:
        mounts = []
        with open("/proc/mounts") as fh:
            for line in fh:
                p = line.split()
                if len(p) >= 2:
                    mounts.append(p[1])
    except Exception:
        return None
    target = os.path.normpath(data_dir)
    if target in mounts:
        return True
    best = "/"
    for mp in mounts:
        if target.startswith(mp.rstrip("/") + "/") and len(mp) > len(best):
            best = mp
    return best != "/"


# ─── Manager: reverse proxy + opencode child supervisor ───────────────────────


def reload_env_from_setup(data_dir):
    """Merge /data/.setup.env into os.environ (wizard output wins over prior env)."""
    for k, v in load_existing(data_dir).items():
        os.environ[k] = v


def run_prep(data_dir):
    """Re-run /prep.sh to regenerate opencode.json, seed skills/AGENTS.md, and
    clone/pull the repo. Called on boot (via entrypoint) and after a /setup save
    so new settings apply without a container restart. Returns True on success."""
    env = dict(os.environ)
    env["DATA_DIR"] = data_dir
    try:
        r = subprocess.run(["/prep.sh"], env=env, capture_output=True, text=True, timeout=180)
    except Exception as e:
        print(f"[manager] prep.sh exception: {e}", flush=True)
        return False
    if r.returncode != 0:
        print(f"[manager] prep.sh failed (rc={r.returncode}): {r.stderr.strip()[:400]}", flush=True)
        return False
    return True


def make_session_cookie(secret):
    exp = int(time.time()) + SESSION_TTL
    payload = f"{exp}".encode()
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_session_cookie(secret, value):
    if not value or "." not in value:
        return False
    exp_s, sig = value.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(secret, f"{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _safe_next(value, default="/manage"):
    """Restrict post-login redirects to same-origin relative paths."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    return value


def _login_url(next_path):
    return "/manage/login?next=" + quote(_safe_next(next_path), safe="")


def _request_path(handler):
    path = urlparse(handler.path).path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path


def _cookie_attrs(handler):
    attrs = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        attrs += "; Secure"
    return attrs


def _session_cookie_header(handler, value, *, clear=False):
    if clear:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    return f"{SESSION_COOKIE}={value}; {_cookie_attrs(handler)}"


# ─── Edge auth helpers (cookie + Basic + auth_token query) ─────────────────────
# The manager accepts three equivalent credentials at the edge:
#   1. oc_session cookie (browser flow — set by POST /manage/login)
#   2. Authorization: Basic opencode:<password> (opencode attach / TUI flow)
#   3. ?auth_token=<password> query param (clients that can't set headers)
# All three resolve to the single shared password. The proxy then re-injects
# its own Basic auth to the child (wizard.py:1856), so the edge credential is
# decoupled from the child's OPENCODE_SERVER_PASSWORD.

OPENCODE_USERNAME = "opencode"


def _check_basic_auth(header_value, password):
    """Return True if `Authorization: Basic ...` decodes to opencode:<password>."""
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:].strip()).decode("utf-8", "replace")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    user, _, pw = decoded.partition(":")
    if user != OPENCODE_USERNAME:
        return False
    return hmac.compare_digest(pw, password)


def _auth_token_from_query(path):
    """Extract ?auth_token=... from the request path, or None."""
    q = parse_qs(urlparse(path).query)
    vals = q.get("auth_token")
    return vals[0] if vals else None


def _is_non_browser_client(handler):
    """True if the request carries Basic auth or an auth_token query param.

    These clients (opencode attach, curl, SDKs) expect a 401 on auth failure,
    not a 302 redirect to an HTML login form they can't render.
    """
    if handler.headers.get("Authorization", "").startswith("Basic "):
        return True
    if _auth_token_from_query(handler.path) is not None:
        return True
    return False


def _strip_auth_token(path):
    """Remove ?auth_token=... from a request path before proxying to the child."""
    parsed = urlparse(path)
    if not parsed.query:
        return path
    q = parse_qs(parsed.query)
    q.pop("auth_token", None)
    # Rebuild query string preserving order is not critical here.
    pairs = []
    for k, vals in q.items():
        for v in vals:
            pairs.append(f"{k}={v}")
    new_query = "&".join(pairs)
    return parsed.path + ("?" + new_query if new_query else "")


# ─── Security headers ──────────────────────────────────────────────────────────

# Headers applied to every manager-served response (login, dashboard, wizard,
# JSON, errors). For proxied opencode responses, only the safe subset is applied
# (see _proxy_security_headers) — we don't impose CSP on the opencode UI.

_BASE_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

# CSP for manager HTML pages (login, dashboard, wizard, error pages). The wizard
# uses inline <style> and a small inline <script> block, so style-src allows
# 'unsafe-inline' (kept tight for scripts — no 'unsafe-inline' for script-src).
_MGR_CSP = (
    "default-src 'self';"
    "style-src 'self' 'unsafe-inline';"
    "script-src 'self' 'unsafe-inline';"
    "img-src 'self' data:;"
    "connect-src 'self';"
    "frame-ancestors 'none'"
)

# Subset applied to proxied responses — no CSP (opencode has its own), but still
# prevent framing and MIME sniffing.
_PROXY_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def _security_headers(handler, *, is_html=False, is_proxy=False):
    """Return a dict of security headers for the response.

    - is_html=True: manager HTML page → include CSP.
    - is_proxy=True: proxied opencode response → safe subset only (no CSP).
    - default: manager JSON/error response → base headers (no CSP needed).
    HSTS is added only when X-Forwarded-Proto is https.
    """
    if is_proxy:
        hdrs = dict(_PROXY_SECURITY_HEADERS)
    else:
        hdrs = dict(_BASE_SECURITY_HEADERS)
        if is_html:
            hdrs["Content-Security-Policy"] = _MGR_CSP
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        hdrs["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return hdrs


# ─── CSRF protection ───────────────────────────────────────────────────────────
# Per-session CSRF token: hmac_sha256(session_secret, "csrf"). Stable for the
# session, rotates when the password changes. For login (no session yet), a
# double-submit nonce cookie is used instead.
#
# CSRF checks apply to manager state-changing POSTs only:
#   /manage/login, /manage/logout, /manage/restart, /manage/revalidate,
#   /manage/keys/rotate, /setup, /test-key, /test-github, /test-mcp
# Proxied opencode API POSTs are NOT checked (they have their own auth via the
# session gate, and opencode attach clients only hit the proxy).

CSRF_COOKIE = "oc_csrf"
_STATE_CHANGING_POSTS = frozenset(
    [
        "/manage/login",
        "/manage/logout",
        "/manage/restart",
        "/manage/revalidate",
        "/manage/keys/rotate",
        "/setup",
        "/test-key",
        "/test-github",
        "/test-mcp",
    ]
)


def _csrf_token_for_secret(secret):
    """CSRF token derived from the session secret. Stable per session."""
    return hmac.new(secret, b"csrf", hashlib.sha256).hexdigest()


def _csrf_token_for_password(password):
    """CSRF token for a Basic-auth/auth_token session (derived from password)."""
    secret = hashlib.sha256(("oc:" + password).encode()).digest()
    return _csrf_token_for_secret(secret)


def _login_csrf_nonce(handler):
    """Get or create a nonce for the login form (double-submit cookie pattern).

    On GET /manage/login we set a random nonce cookie and embed it in the form.
    On POST /manage/login we verify the form nonce matches the cookie nonce.
    Returns (nonce, set_cookie_attr) — set_cookie_attr is the Set-Cookie header
    value to send, or None if the cookie is already present.
    """
    existing = handler._cookies().get(CSRF_COOKIE)
    if existing:
        return existing, None
    nonce = secrets.token_urlsafe(32)
    attrs = f"Path=/; HttpOnly; SameSite=Strict; Max-Age={10 * 60}"  # 10 min for login
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        attrs += "; Secure"
    return nonce, f"{CSRF_COOKIE}={nonce}; {attrs}"


class ChildProcess:
    """Supervises the `opencode web` child on an internal port (127.0.0.1 only)."""

    def __init__(self, data_dir, internal_port, log_ring):
        self.data_dir = data_dir
        self.internal_port = internal_port
        self.log_ring = log_ring
        self.proc = None
        self.stopping = False
        self.crashes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _free_port():
        """Pick an OS-assigned free port on 127.0.0.1 (avoids port+1 collisions)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _cwd(self):
        repo = os.path.join(self.data_dir, "repo")
        return repo if os.path.isdir(os.path.join(repo, ".git")) else self.data_dir

    def _spawn(self):
        self.internal_port = self._free_port()
        env = dict(os.environ)
        env["OPENCODE_SERVER_USERNAME"] = "opencode"
        cmd = ["opencode", "web", "--hostname", "127.0.0.1", "--port", str(self.internal_port)]
        self.proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=self._cwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        print(
            f"[manager] opencode child started (pid={self.proc.pid}, port={self.internal_port})",
            flush=True,
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        try:
            for line in proc.stdout:
                self.log_ring.append(line.rstrip("\n"))
        except Exception:
            pass

    def start(self):
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self._spawn()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while True:
            proc = self.proc
            if proc is None:
                return
            rc = proc.wait()
            print(f"[manager] opencode child exited (rc={rc})", flush=True)
            if self.stopping:
                return
            with self._lock:
                if self.crashes >= CHILD_RESPAWN_MAX:
                    print("[manager] child crashed too many times; giving up", flush=True)
                    self.proc = None
                    return
                self.crashes += 1
                delay = min(2**self.crashes, 30)
            print(f"[manager] respawning child in {delay}s (crash #{self.crashes})", flush=True)
            time.sleep(delay)
            with self._lock:
                if self.stopping:
                    return
                self._spawn()

    def stop(self):
        self.stopping = True
        with self._lock:
            proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

    def restart(self):
        self.crashes = 0
        self.stop()
        self.stopping = False
        with self._lock:
            self._spawn()
        threading.Thread(target=self._watch, daemon=True).start()

    def is_up(self):
        """True if the child is listening on its port (fully ready)."""
        with self._lock:
            proc = self.proc
        if not proc or proc.poll() is not None:
            return False
        c = http.client.HTTPConnection("127.0.0.1", self.internal_port, timeout=2)
        try:
            headers = {}
            pw = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
            if pw:
                tok = base64.b64encode(f"opencode:{pw}".encode()).decode()
                headers["Authorization"] = f"Basic {tok}"
            c.request("GET", "/global/health", headers=headers)
            r = c.getresponse()
            return r.status == 200
        except Exception:
            return False
        finally:
            c.close()

    def is_starting(self):
        """True if the child process is alive but not yet listening (still booting)."""
        with self._lock:
            proc = self.proc
        return bool(proc) and proc.poll() is None and not self.is_up()


class Manager:
    """Server-level state shared across Handler instances (one per request)."""

    def __init__(self, data_dir, port):
        self.data_dir = data_dir
        self.port = port
        self.log_ring = deque(maxlen=LOG_RING)
        self.child = ChildProcess(data_dir, port + 1, self.log_ring)
        self.settings = Settings(data_dir)

    @property
    def password(self):
        return os.environ.get("OPENCODE_SERVER_PASSWORD", "")

    @property
    def configured(self):
        return bool(self.password)

    def session_secret(self):
        return hashlib.sha256(("oc:" + (self.password or "unconfigured")).encode()).digest()

    def start_child(self):
        self.child.start()

    def stop_child(self):
        self.child.stop()

    def restart_child(self):
        self.child.restart()

    def apply_settings(self):
        """After a /setup save: reload env, re-run prep, (re)start the child."""
        reload_env_from_setup(self.data_dir)
        run_prep(self.data_dir)
        with self.child._lock:
            running = self.child.proc and self.child.proc.poll() is None
        if running:
            self.child.restart()
        else:
            self.child.start()


class Settings:
    """Locked wrapper over /data/.setup.env supporting partial updates.

    The on-disk format stays a shell-sourceable KEY=value file so entrypoint/
    prep.sh can still source it. Updates take a threading lock (guards against
    concurrent request threads) and an fcntl lock (guards against other writers).
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, ".setup.env")
        self._lock = threading.Lock()

    def load(self):
        return load_existing(self.data_dir)

    def update(self, mapping):
        """Merge `mapping` (dict) into .setup.env, preserving other keys."""
        with self._lock:
            cur = load_existing(self.data_dir)
            cur.update({k: v for k, v in mapping.items() if v is not None})
            self._write_locked(cur)

    def write(self, mapping):
        with self._lock:
            self._write_locked(mapping)

    def _write_locked(self, mapping):
        lines = ["# Written by opencode manager. Do not commit."]
        for k, v in mapping.items():
            if v is None:
                continue
            lines.append(f"{k}={shlex.quote(str(v))}")
        with open(self.path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            fh.seek(0)
            fh.truncate()
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fchmod(fh.fileno(), 0o600)


# ─── HTML ──────────────────────────────────────────────────────────────────────

CSS = """
  *,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
  :root {
    --bg:#0a0a0b; --panel:#111114; --border:#1e1e24; --border-hi:#2a2a32;
    --text:#e4e4e7; --muted:#71717a; --dim:#52525b;
    --accent:#a1a1aa; --accent-hi:#d4d4d8;
    --ok:#22c55e; --ok-bg:rgba(34,197,94,.08); --ok-bd:rgba(34,197,94,.25);
    --err:#ef4444; --err-bg:rgba(239,68,68,.08); --err-bd:rgba(239,68,68,.25);
    --warn:#f59e0b; --warn-bg:rgba(245,158,11,.08); --warn-bd:rgba(245,158,11,.3);
    --radius:10px;
  }
  body {
    background:var(--bg); color:var(--text);
    font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    min-height:100vh;
  }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,monospace; }
  .wrap { max-width:580px; margin:0 auto; padding:48px 20px 72px; }

  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand-mark { width:28px; height:28px; border:2px solid var(--accent-hi); border-radius:6px; }
  .brand-name { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:18px; font-weight:600; letter-spacing:-.02em; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:32px; }

  .section { border-top:1px solid var(--border); padding:22px 0; }
  .section:first-of-type { border-top:0; padding-top:0; }
  .section-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); margin-bottom:16px; }
  .section-label .num { color:var(--accent); margin-right:6px; }

  .field { margin-bottom:16px; }
  .field:last-child { margin-bottom:0; }
  .field label { display:block; font-size:12px; color:var(--muted); margin-bottom:6px; }
  .field label .opt { color:var(--dim); font-weight:400; }
  input, select {
    width:100%; background:var(--bg); color:var(--text);
    border:1px solid var(--border); border-radius:var(--radius);
    padding:9px 12px; font:inherit; font-size:14px; transition:border-color .15s;
  }
  input:focus, select:focus { outline:none; border-color:var(--border-hi); }
  input::placeholder { color:var(--dim); }
  .hint { font-size:12px; color:var(--dim); margin-top:6px; line-height:1.4; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
    background:var(--bg); padding:1px 5px; border-radius:4px; color:var(--accent-hi); }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media(max-width:520px){ .grid2 { grid-template-columns:1fr; } }

  .row { display:flex; gap:8px; }
  .row > input { flex:1; }
  .btn-inline {
    white-space:nowrap; padding:9px 16px; border:1px solid var(--border);
    background:var(--panel); color:var(--accent-hi); border-radius:var(--radius);
    font:inherit; font-size:13px; cursor:pointer; transition:all .15s;
  }
  .btn-inline:hover { border-color:var(--border-hi); }
  .btn-inline:disabled { opacity:.5; cursor:default; }
  .btn-inline.ok { background:var(--ok-bg); color:var(--ok); border-color:var(--ok-bd); }
  .btn-inline.err { background:var(--err-bg); color:var(--err); border-color:var(--err-bd); }

  .more-btn { font-size:12px; color:var(--accent); background:none; border:0; padding:6px 0;
    cursor:pointer; text-decoration:underline; margin-top:6px; }

  .status { font-size:12px; margin-top:6px; min-height:16px; color:var(--muted); }
  .status.ok { color:var(--ok); }
  .status.err { color:var(--err); }
  .status .count { color:var(--muted); }
  .spinner { display:inline-block; width:12px; height:12px; border:2px solid var(--border-hi);
    border-top-color:var(--accent); border-radius:50%; animation:spin .6s linear infinite; vertical-align:-2px; margin-right:4px; }
  @keyframes spin { to { transform:rotate(360deg); } }

  .model-wrap { position:relative; }
  .model-wrap input { padding-right:32px; }

  .submit-bar { margin-top:28px; }
  .btn-submit {
    width:100%; padding:12px; border:0; border-radius:var(--radius);
    background:var(--accent-hi); color:var(--bg); font:inherit; font-size:15px;
    font-weight:600; cursor:pointer; transition:background .15s;
  }
  .btn-submit:hover { background:#fff; }

  .footer { margin-top:28px; padding:16px; border:1px solid var(--border); border-radius:var(--radius);
    font-size:12px; color:var(--muted); line-height:1.5; }
  .footer strong { color:var(--text); }
  .footer .warn { color:var(--accent-hi); }
  .alert { margin-bottom:24px; padding:12px 14px; border:1px solid var(--warn-bd);
    background:var(--warn-bg); border-radius:var(--radius); font-size:13px; color:var(--warn); }
  .alert.err { border-color:var(--err-bd); background:var(--err-bg); color:var(--err); }
  .alert strong { color:inherit; }

  .checkbox-row { display:flex; align-items:flex-start; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); }
  .checkbox-row:last-child { border-bottom:0; }
  .checkbox-row input[type="checkbox"] { margin-top:3px; width:16px; height:16px; accent-color:var(--accent-hi); flex-shrink:0; }
  .checkbox-row .cb-label { font-size:14px; color:var(--text); }
  .checkbox-row .cb-hint { font-size:12px; color:var(--dim); margin-top:2px; line-height:1.4; }
  .mcp-key { margin:8px 0 4px 24px; }
  .mcp-key .row { display:flex; gap:8px; }
  .mcp-key input { flex:1; }
  .custom-mcp-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:8px; padding:10px; border:1px solid var(--border); border-radius:var(--radius); }
  .custom-mcp-row .full { grid-column:1/-1; }
  .btn-small { font-size:12px; padding:5px 12px; border:1px solid var(--border); background:var(--panel);
    color:var(--accent-hi); border-radius:6px; cursor:pointer; }
  .btn-small:hover { border-color:var(--border-hi); }
  .btn-add { font-size:12px; color:var(--accent); background:none; border:0; padding:6px 0;
    cursor:pointer; text-decoration:underline; }

  .hidden { display:none; }
"""


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect width='24' height='24' rx='5' fill='%230a0a0b'/><rect x='6' y='6' width='12' height='12' fill='none' stroke='%23d4d4d8' stroke-width='2'/></svg>">
<title>opencode &middot; setup</title>
<style>__CSS__</style>
</head><body>
<div class="wrap">
  <div class="brand">
    <div class="brand-mark"></div>
    <div class="brand-name">opencode</div>
  </div>
  <p class="sub">Railway server &middot; first-run configuration</p>

  __VOLUME_ALERT__

  <form method="POST" action="/setup" id="form">
    __CSRF_INPUT__

    <div class="section">
      <div class="section-label"><span class="num">1</span> LLM Provider</div>

      <div class="field">
        <label for="provider">Provider</label>
        <select name="provider" id="provider">
          __PROVIDER_OPTIONS__
        </select>
        <button type="button" class="more-btn" id="more-btn" onclick="toggleMore()">Show all __MORE_COUNT__ providers &rarr;</button>
      </div>

      <div class="field hidden" id="custom-fields">
        <label for="baseurl">Base URL <span class="opt">(OpenAI-compatible)</span></label>
        <input name="baseurl" id="baseurl" placeholder="https://your-endpoint/v1" class="mono">
        <div class="hint">The server's OpenAI-compatible API root. Models are fetched from <code>&lt;base url&gt;/models</code>.</div>
      </div>

      <div class="field hidden" id="customenv-wrap">
        <label for="envvar">API key env var name</label>
        <input name="envvar" id="envvar" placeholder="e.g. MY_GATEWAY_API_KEY" class="mono">
        <div class="hint">The key is stored under this env var name and referenced from <code>opencode.json</code>.</div>
      </div>

      <div class="field">
        <label for="apikey">API key __APIKEY_HINT__</label>
        <div class="row">
          <input name="apikey" id="apikey" type="password" autocomplete="off" placeholder="sk-..." class="mono">
          <button type="button" class="btn-inline" id="test-btn" onclick="testKey()">Test</button>
        </div>
        <div class="status" id="key-status"></div>
      </div>

      <div class="field hidden" id="model-wrap">
        <label for="model">Model id <span class="opt">(required for custom)</span></label>
        <input name="model" id="model" value="__MODEL_VAL__" placeholder="model-id" class="mono">
        <div class="hint" id="model-hint">The model id your custom endpoint serves. opencode lists known providers' models automatically &mdash; pick one with <code>/models</code> after start.</div>
      </div>
    </div>

    <div class="section">
      <div class="section-label"><span class="num">2</span> Project Repository</div>

      <div class="grid2">
        <div class="field">
          <label for="repo">Repo URL <span class="opt">(optional)</span></label>
          <input name="repo" id="repo" value="__REPO_VAL__" placeholder="https://github.com/you/repo" class="mono">
        </div>
        <div class="field">
          <label for="branch">Branch <span class="opt">(optional)</span></label>
          <input name="branch" id="branch" value="__BRANCH_VAL__" placeholder="main" class="mono">
        </div>
      </div>
      <div class="hint" style="margin-top:-8px;margin-bottom:16px">The repo opencode works on inside the container. Leave blank to set later.</div>

      <div class="field">
        <label for="ghtoken">GitHub PAT <span class="opt">(optional)</span></label>
        <div class="row">
          <input name="ghtoken" id="ghtoken" type="password" autocomplete="off" placeholder="github_pat_..." class="mono">
          <button type="button" class="btn-inline" id="gh-btn" onclick="testGitHub()">Test</button>
        </div>
        <div class="status" id="gh-status"></div>
        <div class="hint">Classic PAT with <code>repo</code> scope &mdash; lets the server clone private repos and push.</div>
      </div>
    </div>

    <div class="section">
      <div class="section-label"><span class="num">3</span> Server</div>

      <div class="field">
        <label for="password">Password <span class="opt">(blank = auto)</span></label>
        <input name="password" id="password" value="__PW_VAL__" placeholder="auto-generated" class="mono">
      </div>

      <div class="grid2">
        <div class="field">
          <label for="gitname">Git author name</label>
          <input name="gitname" id="gitname" value="__GITNAME_VAL__" placeholder="opencode">
        </div>
        <div class="field">
          <label for="gitemail">Git author email</label>
          <input name="gitemail" id="gitemail" value="__GITEMAIL_VAL__" placeholder="opencode@railway.local" class="mono">
        </div>
      </div>
    </div>

    <div class="section">
      <div class="section-label"><span class="num">4</span> MCP Servers <span class="opt" style="text-transform:none;font-weight:400">(optional)</span></div>
      <div class="hint" style="margin-bottom:12px">MCP servers add tools the agent can use alongside built-in ones. All optional &mdash; pick what you need.</div>
      __MCP_ROWS__
      <div id="custom-mcp-list" style="margin-top:8px"></div>
      <button type="button" class="btn-add" onclick="addCustomMcp()">+ Add custom remote MCP</button>
    </div>

    <div class="section">
      <div class="section-label"><span class="num">5</span> Agent Skills <span class="opt" style="text-transform:none;font-weight:400">(optional)</span></div>
      <div class="hint" style="margin-bottom:12px">Skills are reusable instructions the agent loads on-demand via the <code>skill</code> tool.</div>
      __SKILL_ROWS__
    </div>

    <div class="submit-bar">
      <button type="submit" class="btn-submit">Save &amp; start opencode</button>
    </div>
  </form>

  <div class="footer">
    <strong>What happens next:</strong> settings are saved to <code>/data/.setup.env</code>,
    <code>opencode.json</code> is written, and <code>opencode web</code> comes up on this domain.
    Log in with username <code>opencode</code> and your password.<br><br>
    <span class="warn">&#9650;</span> For sessions to survive redeploys, add a Railway
    <strong>persistent volume</strong> at <code>/data</code> (Settings &rarr; Volumes).
  </div>
</div>

<script>
const PROVIDERS = __PROVIDERS_JSON__;
const CURATED = __CURATED_JSON__;
const testBtn = document.getElementById('test-btn');
const keyStatus = document.getElementById('key-status');
const modelInput = document.getElementById('model');
const modelWrap = document.getElementById('model-wrap');
const modelHint = document.getElementById('model-hint');
const ghBtn = document.getElementById('gh-btn');
const ghStatus = document.getElementById('gh-status');
const providerSelect = document.getElementById('provider');
const customFields = document.getElementById('custom-fields');
const customenvWrap = document.getElementById('customenv-wrap');
const moreBtn = document.getElementById('more-btn');
let moreShown = false;
let keyTested = false;
let lastTestedKey = '';

function opt(pid, label, sel) {
  const o = document.createElement('option');
  o.value = pid; o.textContent = label;
  if (sel) o.selected = true;
  return o;
}

function renderProviders(selectedPid) {
  const seen = new Set();
  providerSelect.innerHTML = '';
  CURATED.forEach(pid => {
    const p = PROVIDERS[pid]; if (!p) return; seen.add(pid);
    providerSelect.appendChild(opt(pid, p.label, pid === selectedPid));
  });
  providerSelect.appendChild(opt('custom', 'Custom (OpenAI-compatible endpoint)', selectedPid === 'custom'));
}

function toggleMore() {
  const sel = providerSelect.value;
  if (moreShown) {
    renderProviders(sel);
    moreBtn.textContent = 'Show all __MORE_COUNT__ providers \u2192';
    moreShown = false;
  } else {
    const ids = Object.keys(PROVIDERS).sort((a,b) => PROVIDERS[a].label.localeCompare(PROVIDERS[b].label));
    ids.forEach(pid => {
      if (!CURATED.includes(pid) && pid !== 'custom') {
        providerSelect.appendChild(opt(pid, PROVIDERS[pid].label, pid === sel));
      }
    });
    moreBtn.textContent = '\u2190 Show popular only';
    moreShown = true;
  }
}

function isCustom() { return providerSelect.value === 'custom'; }

function toggleCustom() {
  const c = isCustom();
  customFields.classList.toggle('hidden', !c);
  customenvWrap.classList.toggle('hidden', !c);
  modelWrap.classList.toggle('hidden', !c);
}

providerSelect.addEventListener('change', () => {
  toggleCustom();
  keyTested = false; testBtn.className = 'btn-inline'; testBtn.textContent = 'Test';
  keyStatus.textContent = ''; keyStatus.className = 'status';
});

async function testKey() {
  const provider = providerSelect.value;
  const apikey = document.getElementById('apikey').value.trim();
  if (!apikey) { keyStatus.textContent = 'Enter an API key first'; keyStatus.className = 'status err'; return; }
  testBtn.disabled = true; testBtn.textContent = ''; testBtn.className = 'btn-inline';
  const sp = document.createElement('span'); sp.className = 'spinner'; testBtn.appendChild(sp);
  keyStatus.textContent = ''; keyStatus.className = 'status';
  const csrfToken = document.querySelector('input[name="csrf_token"]')?.value || '';
  const body = new URLSearchParams({ provider, apikey, csrf_token: csrfToken });
  if (isCustom()) {
    body.set('baseurl', document.getElementById('baseurl').value.trim());
    body.set('envvar', document.getElementById('envvar').value.trim());
  }
  try {
    const ctrl = new AbortController(); const t = setTimeout(() => ctrl.abort(), 15000);
    const resp = await fetch('/test-key', { method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body, signal: ctrl.signal });
    clearTimeout(t);
    const data = await resp.json();
    if (data.ok) {
      testBtn.textContent = 'Valid'; testBtn.className = 'btn-inline ok';
      keyStatus.textContent = data.message || 'Key valid';
      keyStatus.className = 'status ok';
      keyTested = true; lastTestedKey = apikey;
    } else {
      testBtn.textContent = 'Retry'; testBtn.className = 'btn-inline err';
      keyStatus.textContent = data.error || 'Failed'; keyStatus.className = 'status err';
      keyTested = false;
    }
  } catch (e) {
    testBtn.textContent = 'Retry'; testBtn.className = 'btn-inline err';
    keyStatus.textContent = e.name === 'AbortError' ? 'Timed out' : 'Request failed';
    keyStatus.className = 'status err'; keyTested = false;
  }
  testBtn.disabled = false;
}

async function testGitHub() {
  const token = document.getElementById('ghtoken').value.trim();
  if (!token) { ghStatus.textContent = 'Enter a token first'; ghStatus.className = 'status err'; return; }
  ghBtn.disabled = true; ghBtn.textContent = ''; ghBtn.className = 'btn-inline';
  const sp = document.createElement('span'); sp.className = 'spinner'; ghBtn.appendChild(sp);
  ghStatus.textContent = ''; ghStatus.className = 'status';
  try {
    const ctrl = new AbortController(); const t = setTimeout(() => ctrl.abort(), 12000);
    const resp = await fetch('/test-github', { method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({ token, csrf_token: document.querySelector('input[name="csrf_token"]')?.value || '' }), signal: ctrl.signal });
    clearTimeout(t);
    const data = await resp.json();
    if (data.ok) {
      ghBtn.textContent = 'Valid'; ghBtn.className = 'btn-inline ok';
      let msg = 'Authenticated as ' + data.username;
      if (data.warning) { msg += ' \u2014 ' + data.warning; ghStatus.className = 'status err'; }
      else { ghStatus.className = 'status ok'; }
      ghStatus.textContent = msg;
    } else {
      ghBtn.textContent = 'Retry'; ghBtn.className = 'btn-inline err';
      ghStatus.textContent = data.error || 'Failed'; ghStatus.className = 'status err';
    }
  } catch (e) {
    ghBtn.textContent = 'Retry'; ghBtn.className = 'btn-inline err';
    ghStatus.textContent = 'Request failed'; ghStatus.className = 'status err';
  }
  ghBtn.disabled = false;
}

document.getElementById('form').addEventListener('submit', (e) => {
  const apikey = document.getElementById('apikey').value.trim();
  const alreadySet = document.getElementById('apikey').dataset.alreadySet === '1';
  if (!apikey && !alreadySet) return;
  if (apikey && (!keyTested || lastTestedKey !== apikey)) {
    if (!confirm('You have not tested this API key. Submit anyway?')) e.preventDefault();
  }
});

// ─── MCP + Skills JS ──────────────────────────────────────────────────────────
document.querySelectorAll('.mcp-checkbox').forEach(cb => {
  cb.addEventListener('change', () => {
    const kw = document.getElementById('mcp-key-' + cb.value);
    if (kw) kw.classList.toggle('hidden', !cb.checked);
  });
});

async function testMcp(mcpId) {
  const btn = document.getElementById('mcp-test-' + mcpId);
  const status = document.getElementById('mcp-status-' + mcpId);
  const keyInput = document.getElementById('mcp-key-input-' + mcpId);
  const key = keyInput.value.trim();
  if (!key) { status.textContent = 'Enter a key first'; status.className = 'status err'; return; }
  btn.disabled = true; btn.textContent = ''; btn.className = 'btn-inline';
  const sp = document.createElement('span'); sp.className = 'spinner'; btn.appendChild(sp);
  status.textContent = ''; status.className = 'status';
  try {
    const resp = await fetch('/test-mcp', { method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({ mcp_id: mcpId, key, csrf_token: document.querySelector('input[name="csrf_token"]')?.value || '' }) });
    const data = await resp.json();
    if (data.ok) { btn.textContent = 'Valid'; btn.className = 'btn-inline ok';
      status.textContent = data.detail || 'Connected'; status.className = 'status ok'; }
    else { btn.textContent = 'Retry'; btn.className = 'btn-inline err';
      status.textContent = data.error || 'Failed'; status.className = 'status err'; }
  } catch (e) { btn.textContent = 'Retry'; btn.className = 'btn-inline err';
    status.textContent = 'Request failed'; status.className = 'status err'; }
  btn.disabled = false;
}

let customMcpCount = 0;
function escAttr(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function addCustomMcp(name, url, hdrName, hdrVal) {
  const idx = customMcpCount++;
  const div = document.createElement('div');
  div.className = 'custom-mcp-row';
  div.innerHTML = '<input class="mono" placeholder="Name (e.g. my-server)" data-field="name" value="' + escAttr(name) + '">' +
    '<input class="mono" placeholder="https://..." data-field="url" value="' + escAttr(url) + '">' +
    '<input class="mono" placeholder="Header name (optional)" data-field="hdrname" value="' + escAttr(hdrName) + '">' +
    '<input class="mono" placeholder="Header value (optional)" data-field="hdrval" value="' + escAttr(hdrVal) + '">' +
    '<div class="full"><button type="button" class="btn-small" onclick="this.closest(\'.custom-mcp-row\').remove()">Remove</button></div>';
  document.getElementById('custom-mcp-list').appendChild(div);
}

document.getElementById('form').addEventListener('submit', () => {
  const customs = [];
  document.querySelectorAll('.custom-mcp-row').forEach(row => {
    const name = row.querySelector('[data-field=name]').value.trim();
    const url = row.querySelector('[data-field=url]').value.trim();
    const hn = row.querySelector('[data-field=hdrname]').value.trim();
    const hv = row.querySelector('[data-field=hdrval]').value.trim();
    if (name && url) {
      const entry = { name, url };
      if (hn && hv) entry.headers = { [hn]: hv };
      customs.push(entry);
    }
  });
  let hidden = document.getElementById('mcp-custom-hidden');
  if (!hidden) { hidden = document.createElement('input'); hidden.type = 'hidden';
    hidden.name = 'mcp_custom'; hidden.id = 'mcp-custom-hidden';
    document.getElementById('form').appendChild(hidden); }
  hidden.value = JSON.stringify(customs);
});

__MCP_CUSTOM_PREFILL__

renderProviders(__SELECTED_PID__);
toggleCustom();
</script>
</body></html>"""


SUCCESS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect width='24' height='24' rx='5' fill='%230a0a0b'/><rect x='6' y='6' width='12' height='12' fill='none' stroke='%23d4d4d8' stroke-width='2'/></svg>">
<title>opencode &middot; setup complete</title>
<style>__CSS__
.creds { background:var(--bg); border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin:16px 0; font-size:14px; }
.creds-row { display:flex; justify-content:space-between; align-items:center; padding:6px 0; }
.creds-row + .creds-row { border-top:1px solid var(--border); }
.creds-row .k { color:var(--muted); font-size:12px; }
.creds-row .v { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.copy-btn { font-size:11px; padding:3px 10px; border:1px solid var(--border); background:var(--panel);
  color:var(--accent-hi); border-radius:6px; cursor:pointer; }
.copy-btn:hover { border-color:var(--border-hi); }
.check { color:var(--ok); font-size:22px; }
.pill { display:inline-block; font-size:11px; color:var(--muted); margin-left:8px; }
.pill.ok { color:var(--ok); }
.spinner { display:inline-block; width:11px; height:11px; border:2px solid var(--border-hi);
  border-top-color:var(--accent); border-radius:50%; animation:spin .6s linear infinite; vertical-align:-1px; }
</style></head><body><div class="wrap">
  <div class="brand"><div class="brand-mark"></div><div class="brand-name">opencode</div></div>
  <p class="sub">Setup complete</p>
  <p style="font-size:15px"><span class="check">&#10003;</span> Configuration saved. Starting opencode&hellip;
    <span class="pill" id="status-pill"><span class="spinner"></span> waiting</span></p>
  <div class="creds">
    <div class="creds-row"><span class="k">URL</span><span class="v" id="url">loading...</span></div>
    <div class="creds-row"><span class="k">Username</span><span class="v">opencode</span></div>
    <div class="creds-row"><span class="k">Password</span><span class="v" id="pw">__PW__</span>
      <button class="copy-btn" onclick="copyPw()">Copy</button></div>
  </div>
  <p style="color:var(--muted);font-size:13px" id="reload-msg">This page will redirect automatically once opencode is up.</p>
  <div class="footer">
    <span class="warn">&#9650;</span> Make sure a Railway <strong>persistent volume</strong> is mounted at
    <code>/data</code> so sessions survive redeploys.
  </div>
</div>
<script>
document.getElementById('url').textContent = location.origin;
function copyPw() {
  const pw = document.getElementById('pw').textContent;
  navigator.clipboard.writeText(pw).then(() => {
    const b = event.target; b.textContent = 'Copied'; setTimeout(() => b.textContent = 'Copy', 1500);
  });
}
const pill = document.getElementById('status-pill');
let tries = 0;
async function poll() {
  tries++;
  try {
    const resp = await fetch('/health', { cache: 'no-store' });
    if (resp.ok) {
      pill.innerHTML = 'ready'; pill.className = 'pill ok';
      setTimeout(() => { window.location.href = '/'; }, 1200);
      return;
    }
  } catch (e) {}
  if (tries > 60) { pill.innerHTML = 'timed out'; document.getElementById('reload-msg').textContent =
    'Taking a while \u2014 reload manually in a moment, or check the deploy logs.'; return; }
  setTimeout(poll, 2000);
}
setTimeout(poll, 2000);
</script>
</body></html>"""


# ─── Handler ───────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    data_dir = "/data"
    httpd = None
    manager = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", set_cookies=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookies:
            for sc in set_cookies:
                self.send_header("Set-Cookie", sc)
        is_html = "html" in (ctype or "")
        for k, v in _security_headers(self, is_html=is_html).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = _request_path(self)
        if path in ("/global/health", "/health"):
            # Healthy when: unconfigured (wizard up), child fully up, or child
            # still booting (proc alive). Only unhealthy if the child has crashed
            # and given up — so Railway's healthcheck doesn't restart us during
            # the ~10-20s opencode startup window.
            child = self.manager.child
            healthy = (not self.manager.configured) or child.is_up() or child.is_starting()
            return self._json(200, {"healthy": bool(healthy)})
        # Rate-limit all GETs (except health) to stop scraping/hammering.
        if _rate_limited(_client_ip(self), max_reqs=_RATE_MAX_GET, bucket="get"):
            return self._send(429, "Too many requests — wait a minute and retry.", "text/plain")
        if path == "/manage/login":
            if self.manager.configured and self._authenticated():
                nxt = _safe_next(parse_qs(urlparse(self.path).query).get("next", ["/manage"])[0])
                return self._redirect(nxt)
            return self._render_login()
        if path == "/manage/logout":
            return self._clear_session("/manage/login")
        # Once configured, the manager session is the single auth gate for
        # everything (the opencode UI, reconfigure, management, SSE). The manager
        # injects opencode's basic auth behind the scenes so the user logs in once.
        if self.manager.configured and not self._authenticated():
            if _is_non_browser_client(self):
                return self._send(401, "unauthorized", "text/plain")
            return self._redirect(_login_url(path))
        if path == "/setup":
            return self._render_form()
        if path == "/manage" or path.startswith("/manage/"):
            if not self.manager.configured:
                return self._redirect("/setup")
            return self._manage_get(path)
        if not self.manager.configured:
            if path == "/":
                return self._render_form()
            return self._send(404, "not found", "text/plain")
        return self._proxy("GET")

    def _render_form(self):
        prev = load_existing(self.data_dir)
        providers = get_providers(self.data_dir)
        usable = {pid: c for pid, c in providers.items() if not c["placeholder"]}
        selected = prev.get("OPENCODE_PROVIDER", "anthropic")
        if selected != "custom" and selected not in usable:
            selected = next(iter(usable), "custom")

        curated_present = [pid for pid in CURATED if pid in usable]
        opts = []
        for pid in curated_present:
            c = usable[pid]
            note = " (key set in Railway)" if c["env_var"] and env_var_already_set(c["env_var"]) else ""
            sel = " selected" if pid == selected else ""
            opts.append(f'<option value="{pid}"{sel}>{html.escape(c["label"])}{note}</option>')
        opts.append(f'<option value="custom"{" selected" if selected == "custom" else ""}>Custom (OpenAI-compatible endpoint)</option>')
        provider_options = "\n".join(opts)

        providers_js = json.dumps(
            {pid: {"label": c["label"], "env_var": c["env_var"], "default_model": c["default_model"]} for pid, c in usable.items()}
        )
        curated_js = json.dumps(curated_present)
        more_count = len(usable) - len(curated_present)

        apikey_hint = ""
        already_set = "0"
        if selected != "custom":
            envvar = usable[selected]["env_var"]
            if envvar and env_var_already_set(envvar):
                apikey_hint = '<span class="opt">(already set in Railway &mdash; leave blank to keep)</span>'
                already_set = "1"

        # Model field is shown only for custom providers; show the bare id there.
        default_model_val = prev.get("OPENCODE_MODEL", "")
        if default_model_val.startswith("custom/"):
            default_model_val = default_model_val[len("custom/") :]

        vol = volume_mounted(self.data_dir)
        volume_alert = ""
        if vol is False:
            volume_alert = (
                '<div class="alert err"><strong>No persistent volume detected at /data.</strong> '
                "Sessions, auth and the cloned repo will NOT survive redeploys. Add a Railway "
                "volume mounted at <code>/data</code> (Settings &rarr; Volumes) before relying on this server.</div>"
            )

        # ── MCP rows ──
        enabled_mcps = set()
        for m in (prev.get("ENABLED_MCPS") or "").split(","):
            m = m.strip()
            if m:
                enabled_mcps.add(m)
        mcp_rows = []
        for mid, mc in MCP_CATALOG.items():
            if mc.get("bundled"):
                mcp_rows.append(
                    f'<div class="checkbox-row">'
                    f'<div><div class="cb-label">{html.escape(mc["label"])} '
                    f'<span class="opt" style="text-transform:none;font-weight:400">bundled &middot; on by default</span></div>'
                    f'<div class="cb-hint">{html.escape(mc["hint"])}</div></div></div>'
                )
                continue
            checked = "checked" if mid in enabled_mcps else ""
            row = (
                f'<div class="checkbox-row">'
                f'<input type="checkbox" class="mcp-checkbox" name="mcp" value="{mid}" id="mcp-{mid}" {checked}>'
                f'<div><div class="cb-label">{html.escape(mc["label"])}</div>'
                f'<div class="cb-hint">{html.escape(mc["hint"])}</div>'
            )
            if mc.get("needs_key"):
                key_env = mc["key_env"]
                has_key = bool(prev.get(key_env, ""))
                kd = "" if mid in enabled_mcps else "hidden"
                row += f'<div class="mcp-key {kd}" id="mcp-key-{mid}"><div class="row">'
                row += f'<input type="password" name="mcp_key_{mid}" id="mcp-key-input-{mid}" placeholder="API key" class="mono">'
                if mc.get("test_url"):
                    row += f'<button type="button" class="btn-inline" id="mcp-test-{mid}" onclick="testMcp(\'{mid}\')">Test</button>'
                row += f'</div><div class="status" id="mcp-status-{mid}"></div>'
                if has_key:
                    row += f'<div class="hint">Key already set &mdash; leave blank to keep.</div>'
                row += f"</div>"
            row += f"</div></div>"
            mcp_rows.append(row)
        mcp_rows_html = "\n".join(mcp_rows)

        # ── Skill rows ──
        enabled_skills = set()
        for s in (prev.get("ENABLED_SKILLS") or "").split(","):
            s = s.strip()
            if s:
                enabled_skills.add(s)
        skill_rows = []
        for sid, sdesc in SKILLS.items():
            checked = "checked" if sid in enabled_skills else ""
            skill_rows.append(
                f'<div class="checkbox-row">'
                f'<input type="checkbox" name="skill" value="{sid}" id="skill-{sid}" {checked}>'
                f'<div><div class="cb-label">{html.escape(sid)}</div>'
                f'<div class="cb-hint">{html.escape(sdesc)}</div></div></div>'
            )
        skill_rows_html = "\n".join(skill_rows)

        # ── Custom MCP prefill (raw JSON, valid JS) ──
        mcp_custom_json = "[]"
        custom_raw = prev.get("MCP_CUSTOM", "")
        if custom_raw:
            try:
                customs = json.loads(custom_raw)
                prefill = []
                for c in customs:
                    headers = c.get("headers") or {}
                    hn = ""
                    hv = ""
                    if headers:
                        hn = list(headers.keys())[0]
                        hv = list(headers.values())[0]
                    prefill.append({"name": c.get("name", ""), "url": c.get("url", ""), "hn": hn, "hv": hv})
                mcp_custom_json = json.dumps(prefill)
            except Exception:
                pass
        mcp_custom_prefill_js = (
            f"var __customMcps = {mcp_custom_json};\n__customMcps.forEach(function(c) {{ addCustomMcp(c.name, c.url, c.hn, c.hv); }});"
        )

        page = (
            PAGE.replace("__CSS__", CSS)
            .replace("__VOLUME_ALERT__", volume_alert)
            .replace("__PROVIDER_OPTIONS__", provider_options)
            .replace("__PROVIDERS_JSON__", providers_js)
            .replace("__CURATED_JSON__", curated_js)
            .replace("__MORE_COUNT__", str(more_count))
            .replace("__APIKEY_HINT__", apikey_hint)
            .replace("__SELECTED_PID__", json.dumps(selected))
            .replace("__MODEL_VAL__", html.escape(default_model_val))
            .replace("__REPO_VAL__", html.escape(prev.get("GIT_REPO", "")))
            .replace("__BRANCH_VAL__", html.escape(prev.get("GIT_REPO_BRANCH", "")))
            .replace("__PW_VAL__", html.escape(prev.get("OPENCODE_SERVER_PASSWORD", "")))
            .replace("__GITNAME_VAL__", html.escape(prev.get("GIT_USER_NAME", "opencode")))
            .replace("__GITEMAIL_VAL__", html.escape(prev.get("GIT_USER_EMAIL", "opencode@railway.local")))
            .replace("__MCP_ROWS__", mcp_rows_html)
            .replace("__SKILL_ROWS__", skill_rows_html)
            .replace("__MCP_CUSTOM_PREFILL__", mcp_custom_prefill_js)
        )
        page = page.replace(
            'id="apikey" type="password"',
            f'id="apikey" type="password" data-already-set="{already_set}"',
        )
        csrf_input, csrf_cookie = self._csrf_hidden_input()
        page = page.replace("__CSRF_INPUT__", csrf_input)
        cookies = [csrf_cookie] if csrf_cookie else None
        self._send(200, page, set_cookies=cookies)

    def do_POST(self):
        path = _request_path(self)
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY:
            return self._send(413, "body too large", "text/plain")
        ip = _client_ip(self)
        # Login lockout: if the IP is locked out, reject immediately (before
        # parsing the body or checking CSRF).
        if path == "/manage/login" and _login_locked(ip):
            return self._json(429, {"ok": False, "error": "Too many failed logins — try again later.", "retry_after": 60})
        # Rate-limit all POSTs (state-changing, management, and proxied).
        if path == "/manage/login":
            max_reqs = _RATE_MAX_LOGIN
        else:
            max_reqs = _RATE_MAX_POST
        if _rate_limited(ip, max_reqs=max_reqs, bucket="post"):
            return self._json(429, {"ok": False, "error": "Too many requests \u2014 wait a minute and retry."})
        raw = self.rfile.read(length) if length else b""
        body = raw.decode("utf-8", "replace")
        form = parse_qs(body, keep_blank_values=True)
        f = {k: v[0] for k, v in form.items()}
        f_multi = {k: v for k, v in form.items()}

        # CSRF check on all state-changing POSTs (login uses double-submit
        # nonce; authenticated endpoints use session-derived token).
        if path in _STATE_CHANGING_POSTS and not self._verify_csrf(f):
            return self._send(403, "CSRF token missing or invalid", "text/plain")

        if path == "/manage/login":
            return self._handle_login(f)
        if path == "/manage/logout":
            return self._clear_session("/manage/login")
        # Once configured, the manager session gates everything except login/logout
        # and health (handled in do_GET). This blocks a public domain from driving
        # setup/test/management/proxy endpoints without a session.
        if self.manager.configured and not self._authenticated():
            if path == "/setup":
                if _is_non_browser_client(self):
                    return self._send(401, "unauthorized", "text/plain")
                return self._redirect(_login_url("/setup"))
            return self._json(401, {"ok": False, "error": "unauthenticated"})
        if path.startswith("/manage/"):
            return self._manage_post(path, f, f_multi, raw)
        if path == "/test-key":
            return self._handle_test_key(f)
        if path == "/test-github":
            return self._handle_test_github(f)
        if path == "/test-mcp":
            return self._handle_test_mcp(f)
        if path == "/setup":
            return self._handle_setup(f, f_multi)
        if not self.manager.configured:
            return self._send(404, "not found", "text/plain")
        return self._proxy("POST", body=raw)

    def _handle_test_key(self, f):
        provider = f.get("provider", "")
        apikey = (f.get("apikey") or "").strip()
        providers = get_providers(self.data_dir)
        custom = None
        if provider == "custom":
            custom = {
                "baseurl": (f.get("baseurl") or "").strip(),
                "env": (f.get("envvar") or "").strip().upper(),
            }
        else:
            if provider not in providers:
                return self._json(400, {"ok": False, "error": "Unknown provider"})
            if not apikey:
                envvar = providers[provider].get("env_var", "")
                if envvar and env_var_already_set(envvar):
                    apikey = os.environ[envvar]
                else:
                    return self._json(200, {"ok": False, "error": "Enter an API key"})
        ok, msg = validate_provider_key(provider, apikey, providers, custom)
        if ok:
            return self._json(200, {"ok": True, "message": msg})
        return self._json(200, {"ok": False, "error": msg})

    def _handle_test_github(self, f):
        token = (f.get("token") or "").strip()
        ok, result, scopes = validate_github_token(token)
        if ok:
            payload = {"ok": True, "username": result, "scopes": scopes}
            if "repo" not in [s.strip() for s in (scopes or "").split(",")]:
                payload["warning"] = "Token lacks 'repo' scope \u2014 can't clone private repos or push."
            return self._json(200, payload)
        return self._json(200, {"ok": False, "error": result})

    def _handle_test_mcp(self, f):
        mcp_id = (f.get("mcp_id") or "").strip()
        key = (f.get("key") or "").strip()
        if mcp_id not in MCP_CATALOG:
            return self._json(400, {"ok": False, "error": "Unknown MCP"})
        cfg = MCP_CATALOG[mcp_id]
        url = cfg.get("test_url") or cfg.get("url", "")
        if not url:
            return self._json(200, {"ok": False, "error": "No test endpoint for this MCP"})
        headers = {}
        if key and cfg.get("key_header"):
            val = cfg["key_value_fmt"].replace("{key}", key)
            headers[cfg["key_header"]] = val
        ok, detail = test_mcp_connection(url, headers)
        if ok:
            return self._json(200, {"ok": True, "detail": detail})
        return self._json(200, {"ok": False, "error": detail})

    def _handle_setup(self, f, f_multi=None):
        if f_multi is None:
            f_multi = {}
        providers = get_providers(self.data_dir)
        provider = f.get("provider", "anthropic")
        is_custom = provider == "custom"
        custom = None
        envvar = ""

        if is_custom:
            envvar = (f.get("envvar") or "CUSTOM_API_KEY").strip().upper()
            if not _ENV_VAR_RE.match(envvar):
                return self._send(400, self._err_page("Invalid API key env var name (A-Z, 0-9, _; cannot start with a digit)."))
            baseurl = (f.get("baseurl") or "").strip()
            if not baseurl:
                return self._send(400, self._err_page("A base URL is required for a custom provider."))
            custom = {"id": "custom", "label": "Custom", "baseurl": baseurl, "npm": "@ai-sdk/openai-compatible", "env": envvar}
        else:
            if provider not in providers or providers[provider]["placeholder"]:
                return self._send(400, self._err_page("Unknown provider."))
            envvar = providers[provider]["env_var"]
            if not envvar:
                return self._send(400, self._err_page("This provider has no configured API key env var."))

        apikey = (f.get("apikey") or "").strip()
        if not apikey and not env_var_already_set(envvar):
            return self._send(400, self._err_page("An API key is required."))
        if not apikey and env_var_already_set(envvar):
            apikey = os.environ[envvar]

        password = (f.get("password") or "").strip() or secrets.token_urlsafe(18)
        # Model selection is handled by opencode's /models at runtime for known
        # providers. Custom endpoints aren't in models.dev, so opencode won't
        # know their model ids — require one here and persist it as custom/<id>.
        model = (f.get("model") or "").strip()
        if is_custom:
            if not model:
                return self._send(400, self._err_page("A model id is required for a custom provider."))
            if "/" not in model:
                model = "custom/" + model
        else:
            model = ""  # known providers: let opencode pick via /models
        repo = (f.get("repo") or "").strip()
        branch = (f.get("branch") or "").strip()
        ghtoken = (f.get("ghtoken") or "").strip()
        gitname = (f.get("gitname") or "opencode").strip() or "opencode"
        gitemail = (f.get("gitemail") or "").strip() or "opencode@railway.local"

        env_path = os.path.join(self.data_dir, ".setup.env")
        lines = [
            "# Written by opencode setup wizard. Do not commit.",
            f"OPENCODE_PROVIDER={shlex.quote(provider)}",
            f"OPENCODE_PROVIDER_KEY_ENV={shlex.quote(envvar)}",
            f"{envvar}={shlex.quote(apikey)}",
            f"OPENCODE_SERVER_PASSWORD={shlex.quote(password)}",
            f"GIT_USER_NAME={shlex.quote(gitname)}",
            f"GIT_USER_EMAIL={shlex.quote(gitemail)}",
        ]
        if model:
            lines.append(f"OPENCODE_MODEL={shlex.quote(model)}")
        if repo:
            lines.append(f"GIT_REPO={shlex.quote(repo)}")
        if branch:
            lines.append(f"GIT_REPO_BRANCH={shlex.quote(branch)}")
        if ghtoken:
            lines.append(f"GITHUB_TOKEN={shlex.quote(ghtoken)}")
        if is_custom:
            lines.append(f"OPENCODE_CUSTOM_ID={shlex.quote(custom['id'])}")
            lines.append(f"OPENCODE_CUSTOM_LABEL={shlex.quote(custom['label'])}")
            lines.append(f"OPENCODE_CUSTOM_BASEURL={shlex.quote(custom['baseurl'])}")
            lines.append(f"OPENCODE_CUSTOM_NPM={shlex.quote(custom['npm'])}")
            lines.append(f"OPENCODE_CUSTOM_ENV={shlex.quote(custom['env'])}")

        # MCP servers (opt-in via checkboxes)
        enabled_mcps = f_multi.get("mcp", [])
        if enabled_mcps:
            lines.append(f"ENABLED_MCPS={shlex.quote(','.join(enabled_mcps))}")
            prev_mcp = load_existing(self.data_dir)
            for mid in enabled_mcps:
                cfg_mcp = MCP_CATALOG.get(mid)
                if not cfg_mcp or not cfg_mcp.get("needs_key"):
                    continue
                key_env = cfg_mcp["key_env"]
                mcp_key = (f.get(f"mcp_key_{mid}") or "").strip()
                if not mcp_key:
                    mcp_key = prev_mcp.get(key_env, "")
                if mcp_key:
                    lines.append(f"{key_env}={shlex.quote(mcp_key)}")

        mcp_custom = (f.get("mcp_custom") or "").strip()
        if mcp_custom and mcp_custom != "[]":
            lines.append(f"MCP_CUSTOM={shlex.quote(mcp_custom)}")

        # Skills (opt-in via checkboxes)
        enabled_skills = f_multi.get("skill", [])
        if enabled_skills:
            lines.append(f"ENABLED_SKILLS={shlex.quote(','.join(enabled_skills))}")

        with open(env_path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(env_path, 0o600)

        # Reload env synchronously so os.environ (and thus the session secret for
        # the cookie below) reflects the new password immediately; apply_settings
        # in the background re-runs prep (opencode.json, skills, AGENTS.md, repo)
        # and (re)starts the opencode child without blocking the response.
        reload_env_from_setup(self.data_dir)
        print("[manager] setup saved — applying settings", flush=True)
        threading.Thread(target=self.manager.apply_settings, daemon=True).start()
        # Auto-login: issue a session cookie for the new password so the success
        # page's redirect to / doesn't bounce through /manage/login again.
        secret = hashlib.sha256(("oc:" + password).encode()).digest()
        val = make_session_cookie(secret)
        body = SUCCESS.replace("__CSS__", CSS).replace("__PW__", html.escape(password))
        body_b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            _session_cookie_header(self, val),
        )
        for k, v in _security_headers(self, is_html=True).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body_b)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err_page(self, msg):
        return (
            f'<!doctype html><html><head><meta charset="utf-8">'
            f'<style>{CSS}</style></head><body><div class="wrap">'
            f'<div class="brand"><div class="brand-mark"></div><div class="brand-name">opencode</div></div>'
            f'<p class="sub">Fix this and go back</p>'
            f'<div class="alert err">{html.escape(msg)}</div>'
            f'<p style="margin-top:20px"><a href="/" style="color:var(--accent-hi)">&larr; Back to setup</a></p>'
            f"</div></body></html>"
        )

    # ── auth + session ────────────────────────────────────────────────────────

    def _cookies(self):
        c = {}
        raw = self.headers.get("Cookie", "")
        if raw:
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    c[k.strip()] = v.strip()
        return c

    def _authenticated(self):
        if not self.manager.configured:
            return True
        # 1. Session cookie (browser flow)
        if verify_session_cookie(self.manager.session_secret(), self._cookies().get(SESSION_COOKIE)):
            return True
        # 2. HTTP Basic auth (opencode attach / TUI flow)
        if _check_basic_auth(self.headers.get("Authorization", ""), self.manager.password):
            return True
        # 3. ?auth_token=<password> query param (headerless clients)
        tok = _auth_token_from_query(self.path)
        if tok and hmac.compare_digest(tok, self.manager.password):
            return True
        return False

    def _csrf_token(self):
        """The expected CSRF token for the current authenticated session."""
        return _csrf_token_for_secret(self.manager.session_secret())

    def _csrf_ok(self, f):
        """Verify the CSRF token in the form (or X-CSRF-Token header) matches."""
        expected = self._csrf_token()
        token = (f.get("csrf_token") or "").strip()
        if not token:
            token = (self.headers.get("X-CSRF-Token") or "").strip()
        return bool(token) and hmac.compare_digest(token, expected)

    def _verify_csrf(self, f):
        """Unified CSRF check for all state-changing POSTs.

        - Authenticated (cookie/Basic/auth_token): session-derived token.
        - Unauthenticated (login or first-run): double-submit nonce cookie.
        """
        if self.manager.configured and self._authenticated():
            return self._csrf_ok(f)
        # Double-submit nonce for login / first-run
        cookie_nonce = self._cookies().get(CSRF_COOKIE)
        form_nonce = (f.get("csrf_token") or "").strip()
        if not cookie_nonce or not form_nonce:
            return False
        return hmac.compare_digest(cookie_nonce, form_nonce)

    def _csrf_for_form(self):
        """Return (token, set_cookie) for embedding in a form.

        set_cookie is a Set-Cookie header value to send, or None if no cookie
        needs to be set (already present or session-based).
        """
        if self.manager.configured and self._authenticated():
            return self._csrf_token(), None
        nonce, set_cookie = _login_csrf_nonce(self)
        return nonce, set_cookie

    def _csrf_hidden_input(self):
        """Return the HTML hidden input for the CSRF token, plus any cookie to set."""
        token, set_cookie = self._csrf_for_form()
        return f'<input type="hidden" name="csrf_token" value="{html.escape(token)}">', set_cookie

    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in _security_headers(self).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _clear_session(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Set-Cookie", _session_cookie_header(self, "", clear=True))
        self.send_header("Content-Length", "0")
        for k, v in _security_headers(self).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _handle_login(self, f):
        if not self.manager.configured:
            return self._redirect("/setup")
        pw = f.get("password", "")
        nxt = f.get("next", "/manage") or "/manage"
        ip = _client_ip(self)
        if pw and hmac.compare_digest(pw, self.manager.password):
            _record_login_success(ip)
            val = make_session_cookie(self.manager.session_secret())
            self.send_response(302)
            self.send_header("Location", nxt)
            self.send_header(
                "Set-Cookie",
                _session_cookie_header(self, val),
            )
            self.send_header("Content-Length", "0")
            for k, v in _security_headers(self).items():
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        _record_login_failure(ip)
        return self._render_login(error="Incorrect password.", next_path=nxt)

    def _render_login(self, error="", next_path=None):
        err = f'<div class="alert err">{html.escape(error)}</div>' if error else ""
        if next_path is None:
            nxt = _safe_next(parse_qs(urlparse(self.path).query).get("next", ["/manage"])[0])
        else:
            nxt = _safe_next(next_path)
        csrf_input, csrf_cookie = self._csrf_hidden_input()
        page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head><body><div class="wrap">
<div class="brand"><div class="brand-mark"></div><div class="brand-name">opencode</div></div>
<p class="sub">Railway manager &middot; login</p>
{err}
<form method="POST" action="/manage/login" class="section">
{csrf_input}
<input type="hidden" name="next" value="{html.escape(nxt)}">
<label>Password</label>
<input type="password" name="password" autofocus required>
<button type="submit" class="btn-submit">Log in</button>
</form>
<p class="sub">User is <code>opencode</code>. Use the password set during first-run setup.</p>
</div></body></html>"""
        cookies = [csrf_cookie] if csrf_cookie else None
        self._send(200, page, set_cookies=cookies)

    # ── reverse proxy to the opencode child ───────────────────────────────────

    def _proxy(self, method, body=b""):
        mgr = self.manager
        if not mgr.child.is_up():
            return self._send(503, "opencode is starting up — retry shortly.", "text/plain")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", mgr.child.internal_port, timeout=None)
            out_headers = {}
            for k, v in self.headers.items():
                if k.lower() in HOP_BY_HOP_REQ:
                    continue
                out_headers[k] = v
            out_headers["Host"] = f"127.0.0.1:{mgr.child.internal_port}"
            if mgr.password:
                tok = base64.b64encode(f"opencode:{mgr.password}".encode()).decode()
                out_headers["Authorization"] = f"Basic {tok}"
            out_headers["Connection"] = "close"
            if body:
                out_headers["Content-Length"] = str(len(body))
            # Strip auth_token from the query string before proxying so the
            # edge credential doesn't leak to the child's access logs.
            proxy_path = _strip_auth_token(self.path)
            conn.request(method, proxy_path, body=body, headers=out_headers)
            resp = conn.getresponse()
            self.send_response_only(resp.status, resp.reason)
            existing = {k.lower() for k, _ in resp.getheaders()}
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            # Add security headers (safe subset — no CSP on proxied UI) without
            # duplicating any the child already set.
            for k, v in _security_headers(self, is_proxy=True).items():
                if k.lower() not in existing:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                # SSE (text/event-stream) is line-delimited; read(8192) would block
                # waiting for a full buffer on a slow stream. Use readline() for SSE,
                # read(8192) for everything else (complete responses buffer fine).
                is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "")
                if is_sse:
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        self.wfile.write(line)
                        self.wfile.flush()
                else:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()
            self.close_connection = True
        except Exception as e:
            try:
                self._send(502, f"proxy error: {e}", "text/plain")
            except Exception:
                pass

    # ── management surface ────────────────────────────────────────────────────

    def _resolve_provider_key(self, prev):
        """Return (provider, envvar, apikey) for the configured provider, or None."""
        provider = prev.get("OPENCODE_PROVIDER", "")
        if not provider:
            return None
        envvar = (prev.get("OPENCODE_PROVIDER_KEY_ENV") or "").upper()
        if not envvar:
            # Fallback for older .setup.env files without OPENCODE_PROVIDER_KEY_ENV.
            if provider == "custom":
                envvar = (prev.get("OPENCODE_CUSTOM_ENV") or "CUSTOM_API_KEY").upper()
            else:
                cfg = get_providers(self.data_dir).get(provider)
                envvar = cfg["env_var"] if cfg and cfg.get("env_var") else ""
        if not envvar:
            return provider, "", ""
        return provider, envvar, os.environ.get(envvar, prev.get(envvar, ""))

    def _manage_get(self, path):
        if path == "/manage":
            return self._render_dashboard()
        if path == "/manage/logs":
            return self._json(200, {"lines": list(self.manager.log_ring)})
        if path == "/manage/status":
            prev = load_existing(self.data_dir)
            return self._json(
                200,
                {
                    "configured": self.manager.configured,
                    "child_up": self.manager.child.is_up(),
                    "provider": prev.get("OPENCODE_PROVIDER", ""),
                    "model": prev.get("OPENCODE_MODEL", ""),
                    "repo": prev.get("GIT_REPO", ""),
                    "enabled_mcps": prev.get("ENABLED_MCPS", ""),
                    "enabled_skills": prev.get("ENABLED_SKILLS", ""),
                    "volume_mounted": volume_mounted(self.data_dir),
                },
            )
        return self._send(404, "not found", "text/plain")

    def _manage_post(self, path, f, f_multi, raw):
        if path == "/manage/restart":
            print("[manager] restart requested via /manage", flush=True)
            threading.Thread(target=self.manager.restart_child, daemon=True).start()
            return self._redirect("/manage")
        if path == "/manage/revalidate":
            prev = load_existing(self.data_dir)
            info = self._resolve_provider_key(prev)
            if not info or not info[1] or not info[2]:
                return self._json(200, {"ok": False, "error": "No provider key configured to revalidate."})
            provider, envvar, apikey = info
            providers = get_providers(self.data_dir)
            ok, detail = validate_provider_key(provider, apikey, providers)
            return self._json(200, {"ok": ok, "detail": detail, "envvar": envvar})
        if path == "/manage/keys/rotate":
            envvar = (f.get("envvar") or "").strip().upper()
            apikey = (f.get("apikey") or "").strip()
            if not _ENV_VAR_RE.match(envvar) or not apikey:
                return self._json(400, {"ok": False, "error": "A valid env var name and key are required."})
            self.manager.settings.update({envvar: apikey})
            reload_env_from_setup(self.data_dir)
            print(f"[manager] key rotated for {envvar}; applying + restarting child", flush=True)
            threading.Thread(target=self.manager.apply_settings, daemon=True).start()
            return self._redirect("/manage")
        return self._json(404, {"ok": False, "error": "unknown management action"})

    def _render_dashboard(self):
        mgr = self.manager
        prev = load_existing(self.data_dir)
        up = mgr.child.is_up()
        with mgr.child._lock:
            pid = mgr.child.proc.pid if mgr.child.proc and mgr.child.proc.poll() is None else None
        csrf_token = self._csrf_token()
        csrf_input = f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">'
        rows = [
            ("opencode child", f"{'up' if up else 'down'}" + (f" (pid {pid})" if pid else "")),
            ("Provider", prev.get("OPENCODE_PROVIDER", "—")),
            ("Model", prev.get("OPENCODE_MODEL", "opencode default")),
            ("Repo", prev.get("GIT_REPO", "—") or "—"),
            ("MCPs", prev.get("ENABLED_MCPS", "—") or "— (toolkit only)"),
            ("Skills", prev.get("ENABLED_SKILLS", "—") or "—"),
            ("Volume", "mounted" if volume_mounted(self.data_dir) else "not detected"),
        ]
        rows_html = "\n".join(
            f'<div class="creds-row"><span class="k">{html.escape(k)}</span><span class="v">{html.escape(str(v))}</span></div>'
            for k, v in rows
        )
        logs_html = (
            "\n".join(f'<div class="log-line">{html.escape(line)}</div>' for line in list(mgr.log_ring)[-40:])
            or '<div class="log-line muted">no logs yet</div>'
        )
        key_info = self._resolve_provider_key(prev)
        if key_info and key_info[1]:
            masked = (key_info[2][:4] + "…" + key_info[2][-4:]) if key_info[2] else "—"
            key_section = f"""<h3 style="margin-top:24px;color:var(--accent-hi)">Provider key</h3>
<div class="creds"><div class="creds-row"><span class="k">Env var</span><span class="v">{html.escape(key_info[1])}</span></div>
<div class="creds-row"><span class="k">Current</span><span class="v">{html.escape(masked)}</span></div></div>
<div class="section" style="margin-top:12px">
<button type="button" class="btn-small" id="reval-btn" onclick="revalidate()">Revalidate key</button>
<span id="reval-result" class="muted" style="margin-left:10px"></span>
</div>
<form method="POST" action="/manage/keys/rotate" class="section" style="margin-top:12px">
{csrf_input}
<input type="hidden" name="envvar" value="{html.escape(key_info[1])}">
<label>Rotate key — paste a new {html.escape(key_info[1])}:</label>
<input type="password" name="apikey" placeholder="new key" required>
<button type="submit" class="btn-submit">Rotate &amp; restart</button>
</form>"""
        else:
            key_section = '<p class="sub" style="margin-top:18px">No provider key to manage — configure one via Reconfigure.</p>'
        page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CSS}</style></head><body><div class="wrap">
<div class="brand"><div class="brand-mark"></div><div class="brand-name">opencode</div></div>
<p class="sub">Railway manager &middot; dashboard</p>
<div class="creds">{rows_html}</div>
<div class="section" style="margin-top:20px">
<a class="btn-submit" style="display:inline-block;text-decoration:none;margin-right:10px" href="/setup">Reconfigure</a>
<form method="POST" action="/manage/restart" style="display:inline">
{csrf_input}
<button type="submit" class="btn-submit">Restart opencode</button>
</form>
<a class="btn-small" style="margin-left:10px" href="/manage/logout">Log out</a>
</div>
{key_section}
<h3 style="margin-top:24px;color:var(--accent-hi)">Recent logs</h3>
<pre class="logs">{logs_html}</pre>
<p class="sub" style="margin-top:18px"><a href="/" style="color:var(--accent-hi)">&rarr; Open opencode</a></p>
</div>
<style>.creds-row{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)}}
.creds-row .k{{color:var(--muted)}} .creds-row .v{{color:var(--text);font-family:var(--mono,monospace)}}
.logs{{background:var(--panel);border:1px solid var(--border);padding:12px;max-height:300px;overflow:auto;border-radius:8px}}
.log-line{{font-family:monospace;font-size:12px;white-space:pre-wrap;color:var(--text)}}
.log-line.muted,.muted{{color:var(--muted)}}</style>
<script>async function revalidate(){{const b=document.getElementById('reval-btn');b.disabled=true;const r=document.getElementById('reval-result');
const csrfToken=document.querySelector('input[name="csrf_token"]')?.value||'';
try{{const res=await fetch('/manage/revalidate',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:'csrf_token='+encodeURIComponent(csrfToken)}});const j=await res.json();r.textContent=j.ok?('OK: '+j.detail):('FAIL: '+(j.error||j.detail));r.style.color=j.ok?'var(--ok)':'var(--err,#f87171)'}}
catch(e){{r.textContent='error: '+e}}finally{{b.disabled=false}}}}</script>
</body></html>"""
        self._send(200, page)


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 4096)))
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    ap.add_argument("--manage", action="store_true", help="run as the persistent manager (proxy + child supervisor)")
    args = ap.parse_args()
    Handler.data_dir = args.data
    # Load persisted setup into our env so we know whether we're configured and so
    # the opencode child inherits the provider keys + password.
    reload_env_from_setup(args.data)
    mgr = Manager(args.data, args.port)
    Handler.manager = mgr
    if mgr.configured:
        print("[manager] configured — bringing up opencode child", flush=True)
        mgr.start_child()
    else:
        print("[manager] not configured — serving first-run setup wizard", flush=True)

    def _shutdown(signum, _frame):
        print(f"[manager] signal {signum} — shutting down", flush=True)
        mgr.stop_child()
        if Handler.httpd:
            threading.Thread(target=Handler.httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(
        f"[manager] listening on 0.0.0.0:{args.port} (data={args.data}, configured={mgr.configured})",
        flush=True,
    )
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    Handler.httpd = httpd
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mgr.stop_child()
        sys.exit(0)


if __name__ == "__main__":
    main()
