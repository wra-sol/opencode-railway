#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stdio self-test for the bundled toolkit MCP server.

Spawns mcps/toolkit.py, runs the MCP handshake, lists tools, and asserts
representative tools/call cases across every tool. Pure stdlib. Exits non-zero
on any failure. Run:  python3 mcps/_selftest.py
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "toolkit.py")

PASS = 0
FAIL = 0


def call(proc, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if msg_id is None:
        return None
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


def notify(proc, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def text_of(resp):
    """Return the concatenated text content of a tools/call response."""
    if "error" in resp:
        return f"RPC ERROR: {resp['error']}"
    result = resp.get("result", {})
    return "".join(b.get("text", "") for b in result.get("content", []))


def run_tool(proc, name, args, msg_id):
    resp = call(proc, "tools/call", {"name": name, "arguments": args}, msg_id)
    is_error = resp.get("result", {}).get("isError", False)
    return resp, text_of(resp), is_error


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def expect(label, proc, name, args, predicate, msg_id):
    resp, txt, is_error = run_tool(proc, name, args, msg_id)
    try:
        val = json.loads(txt)
    except Exception:
        val = txt
    ok = (not is_error) and predicate(val, txt)
    check(label, ok, f"isError={is_error} -> {txt[:160]}")


def approx(x, target, tol=1e-9):
    try:
        return abs(float(x) - target) <= tol
    except Exception:
        return False


def main():
    env = dict(os.environ)
    env["OC_MCP_DEBUG"] = ""
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        # ── handshake ──
        init = call(
            proc,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "selftest", "version": "1.0"},
            },
            1,
        )
        check("initialize returns protocolVersion", init.get("result", {}).get("protocolVersion") == "2024-11-05", str(init.get("result")))
        check("initialize advertises tools capability", "tools" in init.get("result", {}).get("capabilities", {}))
        notify(proc, "notifications/initialized")

        # ── ping ──
        ping = call(proc, "ping", None, 2)
        check("ping returns empty result", ping.get("result") == {})

        # ── tools/list ──
        tl = call(proc, "tools/list", None, 3)
        names = [t["name"] for t in tl.get("result", {}).get("tools", [])]
        check("tools/list returns 8 tools", len(names) == 8, f"got {names}")
        expected = {"calculate", "datetime", "text", "generate_id", "convert_units", "semver", "network", "color"}
        check("tools/list has expected names", set(names) == expected, f"got {names}")

        mid = 100
        # ── calculate ──
        mid += 1
        expect(
            "calc: sin(pi/4)",
            proc,
            "calculate",
            {"operation": "eval", "expression": "sin(pi/4)"},
            lambda v, t: approx(v["result"], 0.7071067811865476, 1e-12),
            mid,
        )
        mid += 1
        expect("calc: 2^10 = 1024 (^ is power)", proc, "calculate", {"expression": "2^10"}, lambda v, t: v["result"] == "1024", mid)
        mid += 1
        expect("calc: power via **", proc, "calculate", {"expression": "3**4"}, lambda v, t: v["result"] == "81", mid)
        mid += 1
        expect(
            "calc: constants & physics",
            proc,
            "calculate",
            {"expression": "phi * 2 + c"},
            lambda v, t: v["result"].startswith("299792461"),
            mid,
        )
        mid += 1
        resp, txt, is_err = run_tool(proc, "calculate", {"expression": "(1).__class__"}, mid)
        check("calc: attribute access is blocked", is_err, txt)
        mid += 1
        resp, txt, is_err = run_tool(proc, "calculate", {"expression": "__import__('os')"}, mid)
        check("calc: __import__ is blocked", is_err, txt)
        mid += 1
        expect(
            "calc: base_convert 255 -> hex",
            proc,
            "calculate",
            {"operation": "base_convert", "value": 255, "to_base": 16},
            lambda v, t: v["result"] == "ff",
            mid,
        )
        mid += 1
        expect(
            "calc: base_convert binary -> dec",
            proc,
            "calculate",
            {"operation": "base_convert", "value": "1010", "from_base": 2, "to_base": 10},
            lambda v, t: v["result"] == "10",
            mid,
        )
        mid += 1
        expect(
            "calc: statistics mean",
            proc,
            "calculate",
            {"operation": "statistics", "data": [1, 2, 3, 4, 5], "stat": "mean"},
            lambda v, t: v["result"] == "3",
            mid,
        )
        mid += 1
        expect(
            "calc: statistics stdev",
            proc,
            "calculate",
            {"operation": "statistics", "data": [2, 4, 4, 4, 5, 5, 7, 9], "stat": "stdev"},
            lambda v, t: approx(v["result"], 2.1380, 1e-3),
            mid,
        )
        mid += 1
        expect(
            "calc: is_prime",
            proc,
            "calculate",
            {"operation": "primes", "prime_op": "is_prime", "n": 97},
            lambda v, t: v["is_prime"] is True,
            mid,
        )
        mid += 1
        expect(
            "calc: prime_factors 84",
            proc,
            "calculate",
            {"operation": "primes", "prime_op": "prime_factors", "n": 84},
            lambda v, t: v["factors"] == [2, 2, 3, 7],
            mid,
        )
        mid += 1
        expect("calc: fib(10)=55", proc, "calculate", {"operation": "fib", "n": 10}, lambda v, t: v["fibonacci"] == 55, mid)
        mid += 1
        expect("calc: fib via eval", proc, "calculate", {"expression": "fib(20)"}, lambda v, t: v["result"] == "6765", mid)
        mid += 1
        expect("calc: gcd via eval", proc, "calculate", {"expression": "gcd(48, 36)"}, lambda v, t: v["result"] == "12", mid)

        # ── datetime ──
        mid += 1
        expect(
            "dt: now UTC is ISO",
            proc,
            "datetime",
            {"operation": "now", "timezone": "UTC"},
            lambda v, t: "T" in v["iso"] and v["timezone"] == "UTC",
            mid,
        )
        mid += 1
        expect(
            "dt: now America/New_York",
            proc,
            "datetime",
            {"operation": "now", "timezone": "America/New_York"},
            lambda v, t: "America/New_York" in v["iso"] or "-04" in v["iso"] or "-05" in v["iso"],
            mid,
        )
        mid += 1
        expect(
            "dt: convert tz",
            proc,
            "datetime",
            {"operation": "convert", "datetime": "2026-06-26T12:00:00+00:00", "to_tz": "Asia/Tokyo"},
            lambda v, t: "21:00:00+09:00" in v["result"],
            mid,
        )
        mid += 1
        expect(
            "dt: parse",
            proc,
            "datetime",
            {"operation": "parse", "datetime": "2026-06-26T12:00:00Z"},
            lambda v, t: v["date"] == "2026-06-26" and v["time"] == "12:00:00",
            mid,
        )
        mid += 1
        expect(
            "dt: to_unix", proc, "datetime", {"operation": "to_unix", "datetime": "1970-01-01T00:00:00Z"}, lambda v, t: v["unix"] == 0, mid
        )
        mid += 1
        expect(
            "dt: from_unix",
            proc,
            "datetime",
            {"operation": "from_unix", "timestamp": 0, "timezone": "UTC"},
            lambda v, t: v["iso"].startswith("1970-01-01T00:00:00"),
            mid,
        )
        mid += 1
        expect(
            "dt: add 1 day",
            proc,
            "datetime",
            {"operation": "add", "datetime": "2026-01-01", "value": 1, "unit": "d"},
            lambda v, t: "2026-01-02" in v["result"],
            mid,
        )
        mid += 1
        expect(
            "dt: business_days",
            proc,
            "datetime",
            {"operation": "business_days", "start": "2026-06-01", "end": "2026-06-05"},
            lambda v, t: v["count"] == 5,
            mid,
        )
        mid += 1
        expect(
            "dt: duration_parse 2h30m",
            proc,
            "datetime",
            {"operation": "duration_parse", "duration": "2h30m"},
            lambda v, t: v["seconds"] == 9000,
            mid,
        )
        mid += 1
        expect(
            "dt: duration_humanize 9000",
            proc,
            "datetime",
            {"operation": "duration_humanize", "seconds": 9000},
            lambda v, t: v["human"] == "2h 30m",
            mid,
        )
        mid += 1
        expect(
            "dt: calendar_info",
            proc,
            "datetime",
            {"operation": "calendar_info", "year": 2026, "month": 2},
            lambda v, t: v["days_in_month"] == 28,
            mid,
        )

        # ── text ──
        mid += 1
        expect(
            "text: snake_case",
            proc,
            "text",
            {"operation": "case", "text": "Hello World Foo", "to": "snake"},
            lambda v, t: v["result"] == "hello_world_foo",
            mid,
        )
        mid += 1
        expect(
            "text: kebab",
            proc,
            "text",
            {"operation": "case", "text": "HelloWorld", "to": "kebab"},
            lambda v, t: v["result"] == "hello-world",
            mid,
        )
        mid += 1
        expect(
            "text: slugify",
            proc,
            "text",
            {"operation": "slugify", "text": "Héllo, World!  Foo"},
            lambda v, t: isinstance(v["result"], str) and " " not in v["result"],
            mid,
        )
        mid += 1
        expect(
            "text: base64 roundtrip",
            proc,
            "text",
            {"operation": "encode", "text": "hello", "encoding": "base64"},
            lambda v, t: v["result"] == "aGVsbG8=",
            mid,
        )
        mid += 1
        expect(
            "text: base64 decode",
            proc,
            "text",
            {"operation": "decode", "text": "aGVsbG8=", "encoding": "base64"},
            lambda v, t: v["result"] == "hello",
            mid,
        )
        mid += 1
        expect(
            "text: base58 roundtrip", proc, "text", {"operation": "encode", "text": "hello", "encoding": "base58"}, lambda v, t: True, mid
        )
        mid += 1
        b58resp, b58txt, _ = run_tool(proc, "text", {"operation": "encode", "text": "hello", "encoding": "base58"}, mid)
        b58val = json.loads(b58txt)["result"]
        mid += 1
        expect(
            "text: base58 decode matches",
            proc,
            "text",
            {"operation": "decode", "text": b58val, "encoding": "base58"},
            lambda v, t: v["result"] == "hello",
            mid,
        )
        mid += 1
        expect(
            "text: sha256",
            proc,
            "text",
            {"operation": "hash", "text": "hello", "algorithm": "sha256"},
            lambda v, t: v["result"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            mid,
        )
        mid += 1
        expect(
            "text: hmac sha256",
            proc,
            "text",
            {"operation": "hash", "text": "hello", "algorithm": "sha256", "key": "secret"},
            lambda v, t: v["result"] == "88aab3ede8d3adf94d26ab90d3bafd4a2083070c3bcce9c014ee04a443847c0b",
            mid,
        )
        mid += 1
        expect(
            "text: regex extract",
            proc,
            "text",
            {"operation": "regex", "regex_op": "extract", "pattern": r"\d+", "text": "a1 b22 c333"},
            lambda v, t: v["matches"] == ["1", "22", "333"],
            mid,
        )
        mid += 1
        expect(
            "text: regex replace",
            proc,
            "text",
            {"operation": "regex", "regex_op": "replace", "pattern": r"\d+", "replacement": "#", "text": "a1b2"},
            lambda v, t: v["result"] == "a#b#",
            mid,
        )
        mid += 1
        expect(
            "text: diff",
            proc,
            "text",
            {"operation": "diff", "text_a": "a\nb\nc", "text_b": "a\nx\nc"},
            lambda v, t: "-" in v["diff"] and "+" in v["diff"],
            mid,
        )
        mid += 1
        expect(
            "text: json validate ok",
            proc,
            "text",
            {"operation": "json", "json_op": "validate", "text": '{"a":1}'},
            lambda v, t: v["valid"] is True,
            mid,
        )
        mid += 1
        expect(
            "text: json validate bad",
            proc,
            "text",
            {"operation": "json", "json_op": "validate", "text": "{bad"},
            lambda v, t: v["valid"] is False,
            mid,
        )
        mid += 1
        expect(
            "text: jwt_decode",
            proc,
            "text",
            {"operation": "jwt_decode", "text": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"},
            lambda v, t: v["payload"].get("sub") == "1",
            mid,
        )
        mid += 1
        expect(
            "text: levenshtein",
            proc,
            "text",
            {"operation": "levenshtein", "text_a": "kitten", "text_b": "sitting"},
            lambda v, t: v["distance"] == 3,
            mid,
        )

        # ── generate_id ──
        mid += 1
        expect(
            "id: uuid4",
            proc,
            "generate_id",
            {"operation": "uuid", "version": 4},
            lambda v, t: len(v["uuid"]) == 36 and v["uuid"].count("-") == 4,
            mid,
        )
        mid += 1
        resp1, txt1, _ = run_tool(proc, "generate_id", {"operation": "uuid", "version": 5, "name": "test", "namespace": "dns"}, mid)
        u5a = json.loads(txt1)["uuid"]
        mid += 1
        resp2, txt2, _ = run_tool(proc, "generate_id", {"operation": "uuid", "version": 5, "name": "test", "namespace": "dns"}, mid)
        u5b = json.loads(txt2)["uuid"]
        check("id: uuid5 deterministic", u5a == u5b and len(u5a) == 36, f"{u5a} vs {u5b}")
        mid += 1
        expect("id: ulid 26 chars", proc, "generate_id", {"operation": "ulid"}, lambda v, t: len(v["ulid"]) == 26, mid)
        mid += 1
        expect("id: nanoid default 21", proc, "generate_id", {"operation": "nanoid"}, lambda v, t: len(v["nanoid"]) == 21, mid)
        mid += 1
        expect("id: nanoid custom size", proc, "generate_id", {"operation": "nanoid", "size": 10}, lambda v, t: len(v["nanoid"]) == 10, mid)
        mid += 1
        expect(
            "id: token hex",
            proc,
            "generate_id",
            {"operation": "token", "kind": "hex", "length": 16},
            lambda v, t: len(v["token"]) == 32 and all(c in "0123456789abcdef" for c in v["token"]),
            mid,
        )
        mid += 1
        expect(
            "id: password length", proc, "generate_id", {"operation": "password", "length": 20}, lambda v, t: len(v["password"]) == 20, mid
        )
        mid += 1
        expect(
            "id: random_choice",
            proc,
            "generate_id",
            {"operation": "random_choice", "items": [1, 2, 3]},
            lambda v, t: v["choice"] in [1, 2, 3],
            mid,
        )
        mid += 1
        expect(
            "id: shuffle preserves elements",
            proc,
            "generate_id",
            {"operation": "shuffle", "items": [1, 2, 3, 4]},
            lambda v, t: sorted(v["shuffled"]) == [1, 2, 3, 4],
            mid,
        )

        # ── convert_units ──
        mid += 1
        expect(
            "units: 1 kg -> lb",
            proc,
            "convert_units",
            {"category": "mass", "value": 1, "from_unit": "kg", "to_unit": "lb"},
            lambda v, t: approx(v["result"], 2.2046226218, 1e-6),
            mid,
        )
        mid += 1
        expect(
            "units: 100 C -> F",
            proc,
            "convert_units",
            {"category": "temperature", "value": 100, "from_unit": "c", "to_unit": "f"},
            lambda v, t: v["result"] == "212",
            mid,
        )
        mid += 1
        expect(
            "units: 0 C -> K",
            proc,
            "convert_units",
            {"category": "temperature", "value": 0, "from_unit": "c", "to_unit": "k"},
            lambda v, t: approx(v["result"], 273.15),
            mid,
        )
        mid += 1
        expect(
            "units: 1 km -> mi",
            proc,
            "convert_units",
            {"category": "length", "value": 1, "from_unit": "km", "to_unit": "mi"},
            lambda v, t: approx(v["result"], 0.621371, 1e-5),
            mid,
        )
        mid += 1
        expect(
            "units: 1 GiB -> MiB",
            proc,
            "convert_units",
            {"category": "data", "value": 1, "from_unit": "gib", "to_unit": "mib"},
            lambda v, t: v["result"] == "1024",
            mid,
        )
        mid += 1
        resp, txt, is_err = run_tool(proc, "convert_units", {"category": "bogus", "value": 1, "from_unit": "a", "to_unit": "b"}, mid)
        check("units: bad category is an error", is_err, txt)
        mid += 1
        resp, txt, is_err = run_tool(proc, "convert_units", {"category": "length", "value": 1, "from_unit": "smoot", "to_unit": "m"}, mid)
        check("units: bad unit is an error", is_err, txt)

        # ── semver ──
        mid += 1
        expect(
            "semver: parse",
            proc,
            "semver",
            {"operation": "parse", "version": "1.2.3-beta.1"},
            lambda v, t: v["major"] == 1 and v["minor"] == 2 and v["patch"] == 3 and v["prerelease"] == "beta.1",
            mid,
        )
        mid += 1
        expect("semver: valid", proc, "semver", {"operation": "valid", "version": "1.2.3"}, lambda v, t: v["valid"] is True, mid)
        mid += 1
        expect("semver: invalid", proc, "semver", {"operation": "valid", "version": "1.2"}, lambda v, t: v["valid"] is False, mid)
        mid += 1
        expect("semver: gt", proc, "semver", {"operation": "gt", "a": "1.2.3", "b": "1.2.2"}, lambda v, t: v["gt"] is True, mid)
        mid += 1
        expect(
            "semver: prerelease is lower",
            proc,
            "semver",
            {"operation": "lt", "a": "1.0.0-alpha", "b": "1.0.0"},
            lambda v, t: v["lt"] is True,
            mid,
        )
        mid += 1
        expect(
            "semver: bump minor",
            proc,
            "semver",
            {"operation": "bump", "version": "1.2.3", "kind": "minor"},
            lambda v, t: v["result"] == "1.3.0",
            mid,
        )
        mid += 1
        expect(
            "semver: satisfies ^",
            proc,
            "semver",
            {"operation": "satisfies", "version": "1.5.0", "range": "^1.2.0"},
            lambda v, t: v["satisfies"] is True,
            mid,
        )
        mid += 1
        expect(
            "semver: satisfies fails ^",
            proc,
            "semver",
            {"operation": "satisfies", "version": "2.0.0", "range": "^1.2.0"},
            lambda v, t: v["satisfies"] is False,
            mid,
        )
        mid += 1
        expect(
            "semver: satisfies range with ||",
            proc,
            "semver",
            {"operation": "satisfies", "version": "0.9.0", "range": ">=1.0.0 <2.0.0 || ^0.9.0"},
            lambda v, t: v["satisfies"] is True,
            mid,
        )
        mid += 1
        expect(
            "semver: sort",
            proc,
            "semver",
            {"operation": "sort", "versions": ["1.2.0", "1.10.0", "1.1.0"]},
            lambda v, t: v["sorted"] == ["1.1.0", "1.2.0", "1.10.0"],
            mid,
        )
        mid += 1
        expect(
            "semver: max_satisfying",
            proc,
            "semver",
            {"operation": "max_satisfying", "versions": ["1.2.0", "1.9.0", "2.0.0"], "range": "<2.0.0"},
            lambda v, t: v["max_satisfying"] == "1.9.0",
            mid,
        )

        # ── network ──
        mid += 1
        expect(
            "net: ip_parse",
            proc,
            "network",
            {"operation": "ip_parse", "address": "192.168.1.1"},
            lambda v, t: v["version"] == 4 and v["is_private"] is True,
            mid,
        )
        mid += 1
        expect(
            "net: cidr_info",
            proc,
            "network",
            {"operation": "cidr_info", "cidr": "10.0.0.0/24"},
            lambda v, t: v["num_addresses"] == 256 and v["broadcast"] == "10.0.0.255",
            mid,
        )
        mid += 1
        expect(
            "net: ip_in_cidr yes",
            proc,
            "network",
            {"operation": "ip_in_cidr", "address": "10.0.0.5", "cidr": "10.0.0.0/24"},
            lambda v, t: v["in_cidr"] is True,
            mid,
        )
        mid += 1
        expect(
            "net: ip_in_cidr no",
            proc,
            "network",
            {"operation": "ip_in_cidr", "address": "10.0.1.5", "cidr": "10.0.0.0/24"},
            lambda v, t: v["in_cidr"] is False,
            mid,
        )
        mid += 1
        expect(
            "net: url_parse",
            proc,
            "network",
            {"operation": "url_parse", "url": "https://example.com/a/b?x=1&y=2#frag"},
            lambda v, t: v["scheme"] == "https" and v["path"] == "/a/b" and v["params"]["x"] == "1",
            mid,
        )
        mid += 1
        expect(
            "net: url_resolve",
            proc,
            "network",
            {"operation": "url_resolve", "base": "https://example.com/a/", "ref": "../b"},
            lambda v, t: v["result"] == "https://example.com/b",
            mid,
        )
        mid += 1
        expect(
            "net: query_parse",
            proc,
            "network",
            {"operation": "query_parse", "query": "a=1&b=2&b=3"},
            lambda v, t: v["params"]["a"] == "1" and v["params"]["b"] == ["2", "3"],
            mid,
        )
        mid += 1
        expect(
            "net: ipv6 parse",
            proc,
            "network",
            {"operation": "ip_parse", "address": "::1"},
            lambda v, t: v["version"] == 6 and v["is_loopback"] is True,
            mid,
        )

        # ── color ──
        mid += 1
        expect(
            "color: hex_to_rgb",
            proc,
            "color",
            {"operation": "hex_to_rgb", "hex": "#ff8800"},
            lambda v, t: (v["r"], v["g"], v["b"]) == (255, 136, 0),
            mid,
        )
        mid += 1
        expect(
            "color: rgb_to_hex",
            proc,
            "color",
            {"operation": "rgb_to_hex", "r": 255, "g": 136, "b": 0},
            lambda v, t: v["hex"] == "#ff8800",
            mid,
        )
        mid += 1
        expect(
            "color: rgb_to_hsl red",
            proc,
            "color",
            {"operation": "rgb_to_hsl", "r": 255, "g": 0, "b": 0},
            lambda v, t: v["h"] == 0 and v["s"] == 100,
            mid,
        )
        mid += 1
        expect(
            "color: hsl_to_rgb roundtrip",
            proc,
            "color",
            {"operation": "hsl_to_rgb", "h": 0, "s": 100, "l": 50},
            lambda v, t: v["hex"] == "#ff0000",
            mid,
        )
        mid += 1
        expect(
            "color: contrast_ratio black/white",
            proc,
            "color",
            {"operation": "contrast_ratio", "r1": 0, "g1": 0, "b1": 0, "r2": 255, "g2": 255, "b2": 255},
            lambda v, t: approx(v["ratio"], 21.0, 0.1),
            mid,
        )
        mid += 1
        expect(
            "color: complementary", proc, "color", {"operation": "complementary", "hex": "#ff0000"}, lambda v, t: v["hex"] == "#00ffff", mid
        )
        mid += 1
        expect(
            "color: lighten",
            proc,
            "color",
            {"operation": "lighten", "hex": "#000000", "amount": 0.5},
            lambda v, t: v["hex"] == "#808080",
            mid,
        )
        mid += 1
        expect(
            "color: random hex", proc, "color", {"operation": "random"}, lambda v, t: v["hex"].startswith("#") and len(v["hex"]) == 7, mid
        )

    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        if proc.stderr.read():
            pass

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
