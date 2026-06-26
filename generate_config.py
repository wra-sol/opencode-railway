#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate /data/opencode.json from environment variables.

Reads OPENCODE_MODEL, OPENCODE_SMALL_MODEL, ENABLED_MCPS, MCP_CUSTOM and the
provider/MCP key env vars, then writes opencode's runtime config to
$DATA_DIR/opencode.json. Keys are referenced via {env:VAR} substitution so
secrets stay in the environment (.setup.env) rather than the JSON file.

Called by entrypoint.sh on every boot. Stdlib only.
"""

import json
import os
import sys

# (config_entry, required_env_vars, default_enabled) — MCPs whose required env
# vars are empty are skipped with a warning so opencode doesn't try to connect
# with a blank key. default_enabled=True means the MCP is on out-of-the-box
# regardless of ENABLED_MCPS (used for bundled, key-less local servers); it can
# still be turned off with DISABLE_<NAME>_MCP=1.
MCP_PRESETS = {
    "context7": (
        {"type": "remote", "url": "https://mcp.context7.com/mcp"},
        [],
        False,
    ),
    "gh_grep": (
        {"type": "remote", "url": "https://mcp.grep.app"},
        [],
        False,
    ),
    "tavily": (
        {
            "type": "remote",
            "url": "https://mcp.tavily.com/mcp/",
            "headers": {"Authorization": "Bearer {env:TAVILY_API_KEY}"},
        },
        ["TAVILY_API_KEY"],
        False,
    ),
    "exa": (
        {
            "type": "remote",
            "url": "https://mcp.exa.ai/mcp",
            "headers": {"x-api-key": "{env:EXA_API_KEY}"},
        },
        ["EXA_API_KEY"],
        False,
    ),
    "memory": (
        {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-memory"]},
        [],
        False,
    ),
    "sequential_thinking": (
        {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-sequential-thinking"]},
        [],
        False,
    ),
    "fetch": (
        {"type": "local", "command": ["npx", "-y", "@modelcontextprotocol/server-fetch"]},
        [],
        False,
    ),
    "brave_search": (
        {
            "type": "local",
            "command": ["npx", "-y", "@brave/brave-search-mcp-server"],
            "environment": {"BRAVE_API_KEY": "{env:BRAVE_API_KEY}"},
        },
        ["BRAVE_API_KEY"],
        False,
    ),
    # ── Bundled toolkit ────────────────────────────────────────────────────────
    # Pure-Python local MCP shipped in the image at /mcps/toolkit.py. No key, no
    # network, no npx download — always on unless DISABLE_TOOLKIT_MCP=1. Provides
    # calculate, datetime, text, generate_id, convert_units, semver, network,
    # color tools. See mcps/toolkit.py.
    "toolkit": (
        {"type": "local", "command": ["python3", "/mcps/toolkit.py"]},
        [],
        True,
    ),
}


def build_mcp():
    out = {}
    enabled = [m.strip() for m in os.environ.get("ENABLED_MCPS", "").split(",") if m.strip()]
    for mid in enabled:
        if mid not in MCP_PRESETS:
            print(f"[generate_config] unknown MCP preset: {mid}", file=sys.stderr)
            continue
        entry, required, _default = MCP_PRESETS[mid]
        if os.environ.get(f"DISABLE_{mid.upper()}_MCP", "").strip() in ("1", "true", "yes"):
            print(f"[generate_config] {mid} disabled by DISABLE_{mid.upper()}_MCP", file=sys.stderr)
            continue
        missing = [v for v in required if not os.environ.get(v, "").strip()]
        if missing:
            print(
                f"[generate_config] skipping {mid}: missing env var(s) {', '.join(missing)}",
                file=sys.stderr,
            )
            continue
        out[mid] = dict(entry)
        out[mid]["enabled"] = True

    # Default-enabled bundled MCPs (on regardless of ENABLED_MCPS).
    for mid, (entry, required, default_on) in MCP_PRESETS.items():
        if not default_on or mid in out:
            continue
        if os.environ.get(f"DISABLE_{mid.upper()}_MCP", "").strip() in ("1", "true", "yes"):
            continue
        missing = [v for v in required if not os.environ.get(v, "").strip()]
        if missing:
            continue
        out[mid] = dict(entry)
        out[mid]["enabled"] = True

    custom_raw = os.environ.get("MCP_CUSTOM", "").strip()
    if custom_raw:
        try:
            customs = json.loads(custom_raw)
            if not isinstance(customs, list):
                raise ValueError("MCP_CUSTOM must be a JSON array")
            for c in customs:
                if not isinstance(c, dict):
                    continue
                name = (c.get("name") or "").strip()
                url = (c.get("url") or "").strip()
                if not name or not url:
                    continue
                entry = {"type": "remote", "url": url, "enabled": True}
                headers = c.get("headers") or {}
                if isinstance(headers, dict) and headers:
                    entry["headers"] = headers
                out[name] = entry
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[generate_config] bad MCP_CUSTOM JSON: {e}", file=sys.stderr)
    return out


def build_provider():
    """Build the provider block for custom OpenAI-compatible providers."""
    provider = os.environ.get("OPENCODE_PROVIDER", "").strip()
    if provider != "custom":
        return {}
    cid = os.environ.get("OPENCODE_CUSTOM_ID", "custom").strip() or "custom"
    label = os.environ.get("OPENCODE_CUSTOM_LABEL", "Custom").strip() or "Custom"
    baseurl = os.environ.get("OPENCODE_CUSTOM_BASEURL", "").strip()
    npm = os.environ.get("OPENCODE_CUSTOM_NPM", "@ai-sdk/openai-compatible").strip()
    env_var = os.environ.get("OPENCODE_CUSTOM_ENV", "CUSTOM_API_KEY").strip() or "CUSTOM_API_KEY"
    model = os.environ.get("OPENCODE_MODEL", "").strip()
    bare = model.split("/", 1)[1] if "/" in model else model
    if not baseurl:
        return {}
    return {
        cid: {
            "npm": npm,
            "name": label,
            "options": {
                "baseURL": baseurl,
                "apiKey": "{env:" + env_var + "}",
            },
            "models": {bare: {"name": bare}} if bare else {},
        }
    }


def main():
    data_dir = os.environ.get("DATA_DIR", "/data")
    out_path = os.path.join(data_dir, "opencode.json")

    config = {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
    }
    model = os.environ.get("OPENCODE_MODEL", "").strip()
    small = os.environ.get("OPENCODE_SMALL_MODEL", "").strip()
    if model:
        config["model"] = model
    if small:
        config["small_model"] = small

    provider = build_provider()
    if provider:
        config["provider"] = provider

    mcp = build_mcp()
    if mcp:
        config["mcp"] = mcp

    with open(out_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    n_mcp = len(mcp)
    n_prov = len(provider)
    print(f"[generate_config] wrote {out_path} ({n_mcp} mcp, {n_prov} provider)")


if __name__ == "__main__":
    main()
