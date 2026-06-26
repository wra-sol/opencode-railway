#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""First-run setup wizard for the opencode Railway template.

Served on $PORT by entrypoint.sh when no provider API key is configured.
Collects: LLM provider + key (validated live against the provider's /models
endpoint), model (populated from the provider's model list), repo URL,
GitHub PAT (validated against the GitHub API), server password, and git
identity. Persists to /data/.setup.env, then exits non-zero so Railway
restarts into opencode web. Stdlib only.
"""
import argparse
import html
import json
import os
import shlex
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# ─── Provider configurations ──────────────────────────────────────────────────
# Each provider defines how to authenticate and fetch its model list.
# `auth` is (header_name, value_template). `prefix` is prepended to raw model
# IDs to form opencode's `provider/model` format. `filter` optional to trim
# noise (e.g. OpenAI returns dall-e, whisper, etc). `max_models` caps the list.

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-5",
        "models_url": "https://api.anthropic.com/v1/models",
        "auth": ("x-api-key", "{key}"),
        "extra_headers": {"anthropic-version": "2023-06-01"},
        "prefix": "anthropic/",
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "models_url": "https://api.openai.com/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "openai/",
        "filter": lambda m: any(x in m.lower() for x in ("gpt", "o1", "o3", "o4", "chatgpt")),
    },
    "openrouter": {
        "label": "OpenRouter",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4.5",
        "models_url": "https://openrouter.ai/api/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "openrouter/",
        "max_models": 100,
    },
    "zen": {
        "label": "OpenCode Zen",
        "env_var": "OPENCODE_API_KEY",
        "default_model": "gl-4.7",
        "models_url": None,
        "prefix": "opencode/",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "models_url": "https://api.deepseek.com/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "deepseek/",
    },
    "groq": {
        "label": "Groq",
        "env_var": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "models_url": "https://api.groq.com/openai/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "groq/",
    },
    "xai": {
        "label": "xAI",
        "env_var": "XAI_API_KEY",
        "default_model": "grok-2",
        "models_url": "https://api.x.ai/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "xai/",
    },
    "together": {
        "label": "Together AI",
        "env_var": "TOGETHER_API_KEY",
        "default_model": "",
        "models_url": "https://api.together.xyz/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "together/",
        "max_models": 60,
    },
    "fireworks": {
        "label": "Fireworks AI",
        "env_var": "FIREWORKS_API_KEY",
        "default_model": "",
        "models_url": "https://api.fireworks.ai/inference/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "fireworks/",
        "max_models": 60,
    },
    "cerebras": {
        "label": "Cerebras",
        "env_var": "CEREBRAS_API_KEY",
        "default_model": "",
        "models_url": "https://api.cerebras.ai/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "cerebras/",
    },
    "mistral": {
        "label": "Mistral",
        "env_var": "MISTRAL_API_KEY",
        "default_model": "",
        "models_url": "https://api.mistral.ai/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "mistral/",
    },
    "moonshot": {
        "label": "Moonshot AI",
        "env_var": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-128k",
        "models_url": "https://api.moonshot.cn/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "moonshot/",
    },
    "nvidia": {
        "label": "NVIDIA",
        "env_var": "NVIDIA_API_KEY",
        "default_model": "",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "auth": ("Authorization", "Bearer {key}"),
        "prefix": "nvidia/",
        "max_models": 60,
    },
    "custom": {
        "label": "Custom (enter env var name)",
        "env_var": "",
        "default_model": "",
        "models_url": None,
        "prefix": "",
    },
}

_SSL = ssl.create_default_context()


# ─── API helpers ──────────────────────────────────────────────────────────────

def fetch_provider_models(provider_id, api_key):
    """Validate the key and fetch the model list from the provider's API.
    Returns (ok, models_or_error, note)."""
    cfg = PROVIDERS[provider_id]
    url = cfg.get("models_url")
    if not url:
        return True, [], "No model list API for this provider — enter the model ID manually."

    headers = {}
    auth_h, auth_v = cfg["auth"]
    headers[auth_h] = auth_v.replace("{key}", api_key)
    for k, v in cfg.get("extra_headers", {}).items():
        headers[k] = v
    headers["User-Agent"] = "opencode-railway-wizard/1.0"

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=12, context=_SSL) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid API key (401)", ""
        return False, f"HTTP {e.code}", ""
    except urllib.error.URLError as e:
        return False, "Connection failed", ""
    except Exception as e:
        return False, str(e), ""

    raw = [m.get("id", "") for m in data.get("data", [])]
    raw = [m for m in raw if m]

    filt = cfg.get("filter")
    if filt:
        raw = [m for m in raw if filt(m)]

    raw.sort()

    max_m = cfg.get("max_models", 200)
    truncated = len(raw) > max_m
    raw = raw[:max_m]

    prefix = cfg.get("prefix", "")
    models = [prefix + m for m in raw]

    default = cfg.get("default_model", "")
    if default:
        full = prefix + default
        if full in models:
            models.remove(full)
            models.insert(0, full)

    note = f"Showing first {max_m} — type to search." if truncated else ""
    return True, models, note


def validate_github_token(token):
    """Validate a GitHub PAT by calling /user. Returns (ok, username_or_error)."""
    if not token:
        return False, "No token provided"
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "opencode-railway-wizard/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, data.get("login", "")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Invalid token"
        return False, f"HTTP {e.code}"
    except Exception:
        return False, "Connection failed"


# ─── Existing-config loader ───────────────────────────────────────────────────

def load_existing(data_dir):
    path = os.path.join(data_dir, ".setup.env")
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k] = shlex.split(v)[0] if v else ""
    return out


def env_var_already_set(name):
    return bool(os.environ.get(name))


# ─── HTML ─────────────────────────────────────────────────────────────────────

CSS = """
  *,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
  :root {
    --bg:#0a0a0b; --panel:#111114; --border:#1e1e24; --border-hi:#2a2a32;
    --text:#e4e4e7; --muted:#71717a; --dim:#52525b;
    --accent:#a1a1aa; --accent-hi:#d4d4d8;
    --ok:#22c55e; --ok-bg:rgba(34,197,94,.08); --ok-bd:rgba(34,197,94,.25);
    --err:#ef4444; --err-bg:rgba(239,68,68,.08); --err-bd:rgba(239,68,68,.25);
    --radius:10px;
  }
  body {
    background:var(--bg); color:var(--text);
    font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    min-height:100vh;
  }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,monospace; }
  .wrap { max-width:580px; margin:0 auto; padding:48px 20px 72px; }

  /* header */
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand-mark { width:28px; height:28px; border:2px solid var(--accent-hi); border-radius:6px; }
  .brand-name { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:18px; font-weight:600; letter-spacing:-.02em; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:32px; }

  /* sections */
  .section { border-top:1px solid var(--border); padding:22px 0; }
  .section:first-of-type { border-top:0; padding-top:0; }
  .section-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); margin-bottom:16px; }
  .section-label .num { color:var(--accent); margin-right:6px; }

  /* fields */
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

  /* input + button row */
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

  /* status line */
  .status { font-size:12px; margin-top:6px; min-height:16px; color:var(--muted); }
  .status.ok { color:var(--ok); }
  .status.err { color:var(--err); }
  .status .count { color:var(--muted); }
  .spinner { display:inline-block; width:12px; height:12px; border:2px solid var(--border-hi);
    border-top-color:var(--accent); border-radius:50%; animation:spin .6s linear infinite; vertical-align:-2px; margin-right:4px; }
  @keyframes spin { to { transform:rotate(360deg); } }

  /* datalist wrapper */
  .model-wrap { position:relative; }
  .model-wrap input { padding-right:32px; }

  /* submit */
  .submit-bar { margin-top:28px; }
  .btn-submit {
    width:100%; padding:12px; border:0; border-radius:var(--radius);
    background:var(--accent-hi); color:var(--bg); font:inherit; font-size:15px;
    font-weight:600; cursor:pointer; transition:background .15s;
  }
  .btn-submit:hover { background:#fff; }

  /* info footer */
  .footer { margin-top:28px; padding:16px; border:1px solid var(--border); border-radius:var(--radius);
    font-size:12px; color:var(--muted); line-height:1.5; }
  .footer strong { color:var(--text); }
  .footer .warn { color:var(--accent-hi); }

  /* hidden */
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

  <form method="POST" action="/setup" id="form">

    <div class="section">
      <div class="section-label"><span class="num">1</span> LLM Provider</div>

      <div class="field">
        <label for="provider">Provider</label>
        <select name="provider" id="provider">
          __PROVIDER_OPTIONS__
        </select>
      </div>

      <div class="field hidden" id="customenv-wrap">
        <label for="envvar">Env var name</label>
        <input name="envvar" id="envvar" placeholder="e.g. ANTHROPIC_API_KEY" class="mono">
      </div>

      <div class="field">
        <label for="apikey">API key __APIKEY_HINT__</label>
        <div class="row">
          <input name="apikey" id="apikey" type="password" autocomplete="off" placeholder="sk-..." class="mono">
          <button type="button" class="btn-inline" id="test-btn" onclick="testKey()">Test</button>
        </div>
        <div class="status" id="key-status"></div>
      </div>

      <div class="field">
        <label for="model">Model <span class="opt">(optional)</span></label>
        <div class="model-wrap">
          <input name="model" id="model" value="__MODEL_VAL__" placeholder="provider/model-id" class="mono" list="models-list">
          <datalist id="models-list"></datalist>
        </div>
        <div class="hint" id="model-hint">Test your API key to populate available models, or type manually.</div>
      </div>
    </div>

    <div class="section">
      <div class="section-label"><span class="num">2</span> Project Repository</div>

      <div class="field">
        <label for="repo">Repo URL <span class="opt">(optional)</span></label>
        <input name="repo" id="repo" value="__REPO_VAL__" placeholder="https://github.com/you/repo" class="mono">
        <div class="hint">The repo opencode works on inside the container. Leave blank to set later.</div>
      </div>

      <div class="field">
        <label for="ghtoken">GitHub PAT <span class="opt">(optional)</span></label>
        <div class="row">
          <input name="ghtoken" id="ghtoken" type="password" autocomplete="off" placeholder="github_pat_..." class="mono">
          <button type="button" class="btn-inline" id="gh-btn" onclick="testGitHub()">Test</button>
        </div>
        <div class="status" id="gh-status"></div>
        <div class="hint">Classic PAT with <code>repo</code> scope — lets the server clone private repos and push.</div>
      </div>
    </div>

    <div class="section">
      <div class="section-label"><span class="num">3</span> Server</div>

      <div class="grid2">
        <div class="field">
          <label for="password">Password <span class="opt">(blank = auto)</span></label>
          <input name="password" id="password" value="__PW_VAL__" placeholder="auto-generated" class="mono">
        </div>
        <div class="field">
          <label for="gitname">Git author name</label>
          <input name="gitname" id="gitname" value="__GITNAME_VAL__" placeholder="opencode">
        </div>
      </div>
    </div>

    <div class="submit-bar">
      <button type="submit" class="btn-submit">Save &amp; start opencode</button>
    </div>
  </form>

  <div class="footer">
    <strong>What happens next:</strong> settings are saved to <code>/data/.setup.env</code>,
    the service restarts, and <code>opencode web</code> comes up on this domain.
    Log in with username <code>opencode</code> and your password.<br><br>
    <span class="warn">&#9650;</span> For sessions to survive redeploys, add a Railway
    <strong>persistent volume</strong> at <code>/data</code> (Settings &rarr; Volumes).
  </div>
</div>

<script>
const PROVIDERS = __PROVIDERS_JSON__;
const testBtn = document.getElementById('test-btn');
const keyStatus = document.getElementById('key-status');
const modelInput = document.getElementById('model');
const modelList = document.getElementById('models-list');
const modelHint = document.getElementById('model-hint');
const ghBtn = document.getElementById('gh-btn');
const ghStatus = document.getElementById('gh-status');
const providerSelect = document.getElementById('provider');
const customenvWrap = document.getElementById('customenv-wrap');

function toggleCustom() {
  customenvWrap.classList.toggle('hidden', providerSelect.value !== 'custom');
  updateModelPlaceholder();
}
function updateModelPlaceholder() {
  const p = PROVIDERS[providerSelect.value];
  if (p && p.default_model) {
    const prefix = p.prefix || '';
    modelInput.placeholder = prefix + p.default_model;
  } else {
    modelInput.placeholder = 'provider/model-id';
  }
}
providerSelect.addEventListener('change', toggleCustom);

async function testKey() {
  const provider = providerSelect.value;
  const apikey = document.getElementById('apikey').value.trim();
  if (!apikey) { keyStatus.textContent = 'Enter an API key first'; keyStatus.className = 'status err'; return; }

  testBtn.disabled = true;
  testBtn.textContent = '';
  testBtn.className = 'btn-inline';
  const sp = document.createElement('span'); sp.className = 'spinner'; testBtn.appendChild(sp);
  keyStatus.textContent = ''; keyStatus.className = 'status';

  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 15000);
    const resp = await fetch('/test-key', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({provider, apikey}),
      signal: ctrl.signal,
    });
    clearTimeout(t);
    const data = await resp.json();

    if (data.ok) {
      testBtn.textContent = 'Connected';
      testBtn.className = 'btn-inline ok';

      // populate datalist
      modelList.innerHTML = '';
      (data.models || []).forEach(m => {
        const o = document.createElement('option'); o.value = m; modelList.appendChild(o);
      });

      if (data.models && data.models.length > 0) {
        keyStatus.innerHTML = '<span class="count">' + data.models.length + ' models available</span>';
        keyStatus.className = 'status ok';
        if (!modelInput.value) modelInput.value = data.models[0];
        modelHint.textContent = data.note || 'Select from the list or type to search.';
      } else {
        keyStatus.textContent = 'Key valid';
        keyStatus.className = 'status ok';
        modelHint.textContent = data.note || 'No models returned — enter model ID manually.';
      }
    } else {
      testBtn.textContent = 'Retry';
      testBtn.className = 'btn-inline err';
      keyStatus.textContent = data.error || 'Failed';
      keyStatus.className = 'status err';
    }
  } catch (e) {
    testBtn.textContent = 'Retry';
    testBtn.className = 'btn-inline err';
    keyStatus.textContent = e.name === 'AbortError' ? 'Timed out' : 'Request failed';
    keyStatus.className = 'status err';
  }
  testBtn.disabled = false;
}

async function testGitHub() {
  const token = document.getElementById('ghtoken').value.trim();
  if (!token) { ghStatus.textContent = 'Enter a token first'; ghStatus.className = 'status err'; return; }

  ghBtn.disabled = true;
  ghBtn.textContent = '';
  ghBtn.className = 'btn-inline';
  const sp = document.createElement('span'); sp.className = 'spinner'; ghBtn.appendChild(sp);
  ghStatus.textContent = ''; ghStatus.className = 'status';

  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 12000);
    const resp = await fetch('/test-github', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({token}),
      signal: ctrl.signal,
    });
    clearTimeout(t);
    const data = await resp.json();

    if (data.ok) {
      ghBtn.textContent = 'Valid';
      ghBtn.className = 'btn-inline ok';
      ghStatus.textContent = 'Authenticated as ' + data.username;
      ghStatus.className = 'status ok';
    } else {
      ghBtn.textContent = 'Retry';
      ghBtn.className = 'btn-inline err';
      ghStatus.textContent = data.error || 'Failed';
      ghStatus.className = 'status err';
    }
  } catch (e) {
    ghBtn.textContent = 'Retry';
    ghBtn.className = 'btn-inline err';
    ghStatus.textContent = 'Request failed';
    ghStatus.className = 'status err';
  }
  ghBtn.disabled = false;
}

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
</style></head><body><div class="wrap">
  <div class="brand"><div class="brand-mark"></div><div class="brand-name">opencode</div></div>
  <p class="sub">Setup complete</p>
  <p style="font-size:15px"><span class="check">&#10003;</span> Configuration saved. The service is restarting now.</p>
  <div class="creds">
    <div class="creds-row"><span class="k">URL</span><span class="v" id="url">loading...</span></div>
    <div class="creds-row"><span class="k">Username</span><span class="v">opencode</span></div>
    <div class="creds-row"><span class="k">Password</span><span class="v" id="pw">__PW__</span>
      <button class="copy-btn" onclick="copyPw()">Copy</button></div>
  </div>
  <p style="color:var(--muted);font-size:13px">Reload this page in a few seconds. If the setup form reappears,
  the deployment is still rolling out.</p>
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
</script>
</body></html>"""


# ─── Handler ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    data_dir = "/data"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/global/health", "/health"):
            return self._json(200, {"healthy": True})
        if path not in ("/", "/setup"):
            return self._send(404, "not found", "text/plain")
        self._render_form()

    def _render_form(self):
        prev = load_existing(self.data_dir)
        selected = prev.get("OPENCODE_PROVIDER", "anthropic")
        if selected not in PROVIDERS:
            selected = "anthropic"

        # Build <option> rows
        opts = []
        for pid, cfg in PROVIDERS.items():
            is_sel = " selected" if pid == selected else ""
            note = " (key set in Railway)" if cfg["env_var"] and env_var_already_set(cfg["env_var"]) else ""
            opts.append(f'<option value="{pid}"{is_sel}>{html.escape(cfg["label"])}{note}</option>')
        provider_options = "\n".join(opts)

        # Minimal provider info for the frontend JS (label, prefix, default_model)
        providers_js = json.dumps({
            pid: {"prefix": c.get("prefix", ""), "default_model": c.get("default_model", "")}
            for pid, c in PROVIDERS.items()
        })

        apikey_hint = ""
        envvar = PROVIDERS[selected]["env_var"]
        if envvar and env_var_already_set(envvar):
            apikey_hint = '<span class="opt">(already set in Railway — leave blank to keep)</span>'

        default_model_val = prev.get("OPENCODE_MODEL", "")
        if not default_model_val:
            cfg = PROVIDERS[selected]
            if cfg.get("default_model"):
                default_model_val = cfg["prefix"] + cfg["default_model"]

        page = (
            PAGE
            .replace("__CSS__", CSS)
            .replace("__PROVIDER_OPTIONS__", provider_options)
            .replace("__PROVIDERS_JSON__", providers_js)
            .replace("__APIKEY_HINT__", apikey_hint)
            .replace("__MODEL_VAL__", html.escape(default_model_val))
            .replace("__REPO_VAL__", html.escape(prev.get("GIT_REPO", "")))
            .replace("__PW_VAL__", html.escape(prev.get("OPENCODE_SERVER_PASSWORD", "")))
            .replace("__GITNAME_VAL__", html.escape(prev.get("GIT_USER_NAME", "opencode")))
        )
        self._send(200, page)

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.read_body(length), keep_blank_values=True)
        f = {k: v[0] for k, v in form.items()}

        if path == "/test-key":
            return self._handle_test_key(f)
        if path == "/test-github":
            return self._handle_test_github(f)
        if path == "/setup":
            return self._handle_setup(f)
        self._send(404, "not found", "text/plain")

    def _handle_test_key(self, f):
        provider = f.get("provider", "")
        apikey = (f.get("apikey") or "").strip()
        if provider not in PROVIDERS:
            return self._json(400, {"ok": False, "error": "Unknown provider"})
        if not apikey:
            envvar = PROVIDERS[provider].get("env_var", "")
            if envvar and env_var_already_set(envvar):
                apikey = os.environ[envvar]
            else:
                return self._json(200, {"ok": False, "error": "Enter an API key"})
        ok, result, note = fetch_provider_models(provider, apikey)
        if ok:
            return self._json(200, {"ok": True, "models": result, "note": note, "count": len(result)})
        return self._json(200, {"ok": False, "error": result})

    def _handle_test_github(self, f):
        token = (f.get("token") or "").strip()
        ok, result = validate_github_token(token)
        if ok:
            return self._json(200, {"ok": True, "username": result})
        return self._json(200, {"ok": False, "error": result})

    def _handle_setup(self, f):
        provider = f.get("provider", "anthropic")
        if provider not in PROVIDERS:
            provider = "anthropic"

        if provider == "custom":
            envvar = (f.get("envvar") or "").strip().upper()
            if not envvar or not envvar.replace("_", "").isalnum():
                return self._send(400, self._err_page("Invalid custom env var name."))
        else:
            envvar = PROVIDERS[provider]["env_var"]

        apikey = (f.get("apikey") or "").strip()
        if not apikey and not env_var_already_set(envvar):
            return self._send(400, self._err_page("An API key is required for " + PROVIDERS[provider]["label"] + "."))
        if not apikey and env_var_already_set(envvar):
            apikey = os.environ[envvar]

        password = (f.get("password") or "").strip() or secrets.token_urlsafe(18)
        model = (f.get("model") or "").strip()
        repo = (f.get("repo") or "").strip()
        ghtoken = (f.get("ghtoken") or "").strip()
        gitname = (f.get("gitname") or "opencode").strip() or "opencode"
        gitemail = os.environ.get("GIT_USER_EMAIL", "opencode@railway.local")

        env_path = os.path.join(self.data_dir, ".setup.env")
        lines = [
            "# Written by opencode setup wizard. Do not commit.",
            f"OPENCODE_PROVIDER={shlex.quote(provider)}",
            f"{envvar}={shlex.quote(apikey)}",
            f"OPENCODE_SERVER_PASSWORD={shlex.quote(password)}",
            f"GIT_USER_NAME={shlex.quote(gitname)}",
            f"GIT_USER_EMAIL={shlex.quote(gitemail)}",
        ]
        if model:
            lines.append(f"OPENCODE_MODEL={shlex.quote(model)}")
        if repo:
            lines.append(f"GIT_REPO={shlex.quote(repo)}")
        if ghtoken:
            lines.append(f"GITHUB_TOKEN={shlex.quote(ghtoken)}")
        with open(env_path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(env_path, 0o600)

        body = SUCCESS.replace("__CSS__", CSS).replace("__PW__", html.escape(password))
        self._send(200, body)
        time.sleep(1.5)
        print("[wizard] Setup saved. Restarting into opencode web.", flush=True)
        os._exit(1)

    def read_body(self, length):
        return self.rfile.read(length).decode("utf-8", "replace") if length else ""

    def _err_page(self, msg):
        return (
            f'<!doctype html><html><head><meta charset="utf-8">'
            f'<style>{CSS}</style></head><body><div class="wrap">'
            f'<div class="brand"><div class="brand-mark"></div><div class="brand-name">opencode</div></div>'
            f'<p class="sub">Fix this and go back</p>'
            f'<div class="footer" style="color:var(--err)">{html.escape(msg)}</div>'
            f'<p style="margin-top:20px"><a href="/" style="color:var(--accent-hi)">&larr; Back to setup</a></p>'
            f'</div></body></html>'
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 4096)))
    ap.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    args = ap.parse_args()
    Handler.data_dir = args.data
    print(f"[wizard] listening on 0.0.0.0:{args.port} (data={args.data})", flush=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
