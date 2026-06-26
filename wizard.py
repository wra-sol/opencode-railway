#!/usr/bin/env python3
"""First-run setup wizard for the opencode Railway template.

Served on $PORT by entrypoint.sh when no provider API key is configured.
Collects: LLM provider + key, repo URL, GitHub PAT (optional), server password,
model, and git identity. Persists everything to /data/.setup.env (shell-sourceable)
and /data/opencode.json, then exits non-zero so Railway restarts the deployment
into opencode web. Stdlib only.
"""
import argparse
import html
import json
import os
import shlex
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# provider id -> (label, env_var_name, suggested_model_id_or_'')
PROVIDERS = {
    "anthropic":  ("Anthropic",      "ANTHROPIC_API_KEY",   "anthropic/claude-sonnet-4-5"),
    "openai":     ("OpenAI",         "OPENAI_API_KEY",      "openai/gpt-5.2"),
    "openrouter": ("OpenRouter",     "OPENROUTER_API_KEY",  "openrouter/anthropic/claude-sonnet-4.5"),
    "zen":        ("OpenCode Zen",   "OPENCODE_API_KEY",    "opencode/gl-4.7"),
    "deepseek":   ("DeepSeek",       "DEEPSEEK_API_KEY",    "deepseek/deepseek-v4-pro"),
    "groq":       ("Groq",           "GROQ_API_KEY",        "groq/qwen3-coder-480b"),
    "xai":        ("xAI",            "XAI_API_KEY",         "xai/grok-4"),
    "moonshot":   ("Moonshot AI",    "MOONSHOT_API_KEY",    "moonshotai/kimi-k2"),
    "mistral":    ("Mistral",        "MISTRAL_API_KEY",     ""),
    "together":   ("Together AI",    "TOGETHER_API_KEY",    ""),
    "fireworks":  ("Fireworks AI",   "FIREWORKS_API_KEY",   ""),
    "cerebras":   ("Cerebras",       "CEREBRAS_API_KEY",    ""),
    "nvidia":     ("NVIDIA",         "NVIDIA_API_KEY",      ""),
    "custom":     ("Custom (set env var name)", "",         ""),
}


def load_existing(data_dir):
    """Read prior /data/.setup.env so re-running the wizard keeps old values."""
    path = os.path.join(data_dir, ".setup.env")
    out = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k] = v
    return out


def env_var_already_set(name):
    return bool(os.environ.get(name))


def provider_options(selected):
    rows = []
    for pid, (label, envvar, _model) in PROVIDERS.items():
        is_sel = " selected" if pid == selected else ""
        note = " (key already set in Railway)" if envvar and env_var_already_set(envvar) else ""
        rows.append(f'<option value="{pid}"{is_sel}>{html.escape(label)}{note}</option>')
    return "\n".join(rows)


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>opencode &middot; Railway setup</title>
<style>
  :root {{ --bg:#0d0d0f; --panel:#16161a; --line:#26262c; --txt:#e7e7ea; --mut:#8a8a93; --acc:#cfcecd; --ok:#3fb950; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--txt); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  .wrap {{ max-width:640px; margin:0 auto; padding:40px 20px 80px; }}
  h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:-.01em; }}
  .sub {{ color:var(--mut); margin:0 0 28px; }}
  .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:24px; }}
  label {{ display:block; font-size:13px; color:var(--mut); margin:18px 0 6px; }}
  label:first-child {{ margin-top:0; }}
  input, select {{ width:100%; background:#0a0a0c; color:var(--txt); border:1px solid var(--line);
    border-radius:8px; padding:10px 12px; font:inherit; }}
  input:focus, select:focus {{ outline:none; border-color:#3a3a42; }}
  .hint {{ font-size:12px; color:var(--mut); margin:6px 0 0; }}
  .row2 {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  button {{ margin-top:24px; width:100%; background:var(--acc); color:#0d0d0f; border:0; border-radius:8px;
    padding:12px 16px; font:inherit; font-weight:600; cursor:pointer; }}
  button:hover {{ background:#fff; }}
  .box {{ margin-top:22px; padding:14px 16px; border:1px solid var(--line); border-radius:8px; background:#0a0a0c; font-size:13px; color:var(--mut); }}
  code {{ color:var(--txt); background:#0a0a0c; padding:1px 5px; border-radius:4px; }}
  .k {{ color:var(--ok); }}
  #customenv {{ display:none; }}
</style></head><body>
<div class="wrap">
  <h1>opencode server &middot; first-run setup</h1>
  <p class="sub">One-time configuration. Saved to the persistent volume at <code>/data</code>.</p>
  <div class="panel">
    <form method="POST" action="/setup">
      <label for="provider">LLM provider</label>
      <select name="provider" id="provider" onchange="toggleCustom()">
        __PROVIDER_OPTIONS__
      </select>

      <div id="customenv">
        <label for="envvar">Provider env var name</label>
        <input name="envvar" id="envvar" placeholder="e.g. ANTHROPIC_API_KEY">
      </div>

      <label for="apikey">Provider API key __APIKEY_HINT__</label>
      <input name="apikey" id="apikey" type="password" autocomplete="off" placeholder="sk-...">

      <label for="model">Model <span style="color:var(--mut)">(optional)</span></label>
      <input name="model" id="model" value="__MODEL_VAL__" placeholder="provider/model-id (blank = opencode default)">

      <div class="row2">
        <div>
          <label for="repo">Project repo URL <span style="color:var(--mut)">(optional)</span></label>
          <input name="repo" id="repo" value="__REPO_VAL__" placeholder="https://github.com/you/repo">
        </div>
        <div>
          <label for="ghtoken">GitHub PAT <span style="color:var(--mut)">(optional)</span></label>
          <input name="ghtoken" id="ghtoken" type="password" autocomplete="off" placeholder="github_pat_... (private repos)">
        </div>
      </div>
      <p class="hint">A Classic PAT with <code>repo</code> scope lets the server clone any repo you can access.</p>

      <div class="row2">
        <div>
          <label for="password">Server password</label>
          <input name="password" id="password" value="__PW_VAL__" placeholder="leave blank to auto-generate">
        </div>
        <div>
          <label for="gitname">Git author name</label>
          <input name="gitname" id="gitname" value="__GITNAME_VAL__" placeholder="opencode">
        </div>
      </div>

      <button type="submit">Save &amp; start opencode</button>
    </form>
  </div>
  <div class="box">
    <strong>What happens next:</strong> your settings are written to <code>/data/.setup.env</code>,
    the service restarts, and <code>opencode web</code> comes up on this domain.
    Log in with username <code>opencode</code> and the password above.<br><br>
    <span class="k">&#9650;</span> For sessions to survive redeploys, add a Railway
    <strong>persistent volume</strong> mounted at <code>/data</code> (Settings &rarr; Volumes).
  </div>
</div>
<script>
function toggleCustom(){{
  var p=document.getElementById('provider').value;
  document.getElementById('customenv').style.display = (p==='custom')?'block':'none';
}}
</script>
</body></html>"""


SUCCESS = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>opencode &middot; setup complete</title>
<style>
  body{{margin:0;background:#0d0d0f;color:#e7e7ea;font:15px/1.5 system-ui,sans-serif}}
  .w{{max-width:560px;margin:0 auto;padding:80px 20px}} h1{{font-size:22px}}
  .p{{background:#16161a;border:1px solid #26262c;border-radius:12px;padding:24px;margin-top:20px}}
  code{{background:#0a0a0c;padding:1px 5px;border-radius:4px}} .k{{color:#3fb950}}
</style></head><body><div class="w">
  <h1><span class="k">&#10003;</span> Setup saved</h1>
  <div class="p">
    <p>Configuration written to <code>/data/.setup.env</code>. The service is restarting now.</p>
    <p>When it comes back up, open this same URL and log in:</p>
    <p>Username: <code>opencode</code><br>Password: <code>__PW__</code></p>
    <p style="color:#8a8a93;font-size:13px">Reload this page in a few seconds. If it still shows setup,
    the deployment is still rolling out.</p>
  </div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    data_dir = "/data"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/global/health", "/health"):
            return self._send(200, '{"healthy":true}', "application/json")
        if self.path.split("?")[0] not in ("/", "/setup"):
            return self._send(404, "not found", "text/plain")
        self._render_form()

    def _render_form(self):
        prev = load_existing(self.data_dir)
        selected = prev.get("OPENCODE_PROVIDER", "anthropic")
        if selected not in PROVIDERS:
            selected = "anthropic"
        pw = prev.get("OPENCODE_SERVER_PASSWORD", "")
        # If a provider key is already set in Railway env, hint that it's optional.
        apikey_hint = ""
        page = (
            PAGE
            .replace("__PROVIDER_OPTIONS__", provider_options(selected))
            .replace("__APIKEY_HINT__", "")
            .replace("__MODEL_VAL__", html.escape(prev.get("OPENCODE_MODEL", PROVIDERS[selected][2])))
            .replace("__REPO_VAL__", html.escape(prev.get("GIT_REPO", "")))
            .replace("__PW_VAL__", html.escape(pw))
            .replace("__GITNAME_VAL__", html.escape(prev.get("GIT_USER_NAME", "opencode")))
        )
        self._send(200, page)

    def do_POST(self):
        if self.path.split("?")[0] != "/setup":
            return self._send(404, "not found", "text/plain")
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.read_body(length), keep_blank_values=True)
        f = {k: v[0] for k, v in form.items()}

        provider = f.get("provider", "anthropic")
        if provider not in PROVIDERS:
            provider = "anthropic"

        # Resolve the provider's native env var name.
        if provider == "custom":
            envvar = (f.get("envvar") or "").strip().upper()
            if not envvar or not envvar.replace("_", "").isalnum():
                return self._send(400, "Invalid custom env var name.")
        else:
            envvar = PROVIDERS[provider][1]

        apikey = (f.get("apikey") or "").strip()
        if not apikey and not env_var_already_set(envvar):
            return self._send(400, self._err_page("An API key is required for " + provider + "."))
        if not apikey and env_var_already_set(envvar):
            apikey = os.environ[envvar]  # keep the Railway-provided value

        password = (f.get("password") or "").strip() or secrets.token_urlsafe(18)
        model = (f.get("model") or "").strip()
        repo = (f.get("repo") or "").strip()
        ghtoken = (f.get("ghtoken") or "").strip()
        gitname = (f.get("gitname") or "opencode").strip() or "opencode"
        gitemail = os.environ.get("GIT_USER_EMAIL", "opencode@railway.local")

        # ---- Persist to /data/.setup.env (shell-sourceable) -------------------
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

        # ---- Respond, then hand off to opencode via restart -------------------
        body = SUCCESS.replace("__PW__", html.escape(password))
        self._send(200, body)
        time.sleep(1.5)  # let the response flush to the browser
        print("[wizard] Setup saved. Restarting into opencode web.", flush=True)
        os._exit(1)  # non-zero => Railway restarts the deployment => entrypoint => opencode

    def read_body(self, length):
        return self.rfile.read(length).decode("utf-8", "replace") if length else ""

    def _err_page(self, msg):
        return (
            '<!doctype html><html><body style="background:#0d0d0f;color:#e7e7ea;font:15px/1.5 system-ui">'
            f'<div style="max-width:480px;margin:80px auto;padding:24px;background:#16161a;border:1px solid #26262c;border-radius:12px">'
            f'<h2 style="margin-top:0">Fix this and go back</h2><p>{html.escape(msg)}</p>'
            '<p><a href="/" style="color:#cfcecd">&larr; Back to setup</a></p></div></body></html>'
        )


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
