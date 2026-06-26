#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bundled local MCP toolkit for opencode-railway.

Pure Python standard library — no pip packages, no network, no API keys, no
Node. Speaks MCP over stdio (newline-delimited JSON-RPC 2.0).

Bundled tools (exposed to the LLM as ``toolkit_<name>``):
  calculate       safe expression eval, base conversion, statistics, primes, fib
  datetime        now/convert/parse/format in any IANA zone, business days, durations
  text            case/encode/decode/hash/regex/diff/json/jwt/levenshtein/lorem
  generate_id     uuid / ulid / nanoid / secrets tokens / passwords / random
  convert_units   length, mass, temperature, data, speed, angle, pressure, volume, area, time
  semver          parse / compare / bump / satisfies(range) / sort
  network         ip + cidr info, url parse/build/resolve/encode, query parse
  color           hex/rgb/hsl, contrast ratio, luminance, mix, complement, random

Enabled by default via generate_config.py. Disable with DISABLE_TOOLKIT_MCP=1.
"""

import ast
import base64
import binascii
import difflib
import hashlib
import hmac
import ipaddress
import json
import math
import operator
import os
import random as _random
import re
import secrets
import statistics
import string as _string
import sys
import time
import urllib.parse
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "toolkit"
SERVER_VERSION = "1.0.0"


# ─── stdio helpers ─────────────────────────────────────────────────────────────


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _log(msg):
    if os.environ.get("OC_MCP_DEBUG"):
        sys.stderr.write(f"[toolkit] {msg}\n")
        sys.stderr.flush()


class ToolError(Exception):
    """Raised by a tool to produce an MCP error result (isError: true)."""


def _sanitize(o):
    """Replace non-finite floats so JSON output stays valid (no NaN/Infinity)."""
    if isinstance(o, bool):
        return o
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return o
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(x) for x in o]
    return o


def _to_text(result):
    """Turn a tool's Python return value into MCP text content."""
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(_sanitize(result), indent=2, default=str, ensure_ascii=False)
    return [{"type": "text", "text": text}]


# ─── safe calculator ───────────────────────────────────────────────────────────

_CALC_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CALC_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CALC_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _fib(n):
    if not isinstance(n, int) or n < 0:
        raise ValueError("fib requires a non-negative integer")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def _is_prime(n):
    if not isinstance(n, int) or n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def _prime_factors(n):
    if not isinstance(n, int) or n < 2:
        return []
    out = []
    while n % 2 == 0:
        out.append(2)
        n //= 2
    i = 3
    while i * i <= n:
        while n % i == 0:
            out.append(i)
            n //= i
        i += 2
    if n > 1:
        out.append(n)
    return out


def _calc_namespace():
    ns = {}
    for k, v in vars(math).items():
        if not k.startswith("_") and (callable(v) or isinstance(v, (int, float))):
            ns[k] = v
    for k, v in vars(__import__("cmath")).items():
        if not k.startswith("_") and (callable(v) or isinstance(v, (int, float, complex))) and k not in ns:
            ns[k] = v
    for k, v in vars(statistics).items():
        if not k.startswith("_") and callable(v):
            ns[k] = v
    ns.update(
        {
            "abs": abs,
            "round": round,
            "min": min,
            "max": max,
            "sum": sum,
            "pow": pow,
            "len": len,
            "range": lambda *a: list(range(*a)),
            "int": int,
            "float": float,
            "bool": bool,
            "sorted": sorted,
            "phi": (1 + math.sqrt(5)) / 2,
            "golden": (1 + math.sqrt(5)) / 2,
            "fib": _fib,
            "fibonacci": _fib,
            "is_prime": _is_prime,
            "prime_factors": _prime_factors,
            "bit_and": lambda a, b: a & b,
            "bit_or": lambda a, b: a | b,
            "bit_xor": lambda a, b: a ^ b,
            "bit_not": lambda a: ~a,
            "shl": lambda a, n: a << n,
            "shr": lambda a, n: a >> n,
            "g": 9.80665,
            "G": 6.6743e-11,
            "c": 299792458,
            "h": 6.62607015e-34,
            "hbar": 1.054571817e-34,
            "k": 1.380649e-23,
            "kB": 1.380649e-23,
            "na": 6.02214076e23,
            "NA": 6.02214076e23,
            "R": 8.314462618,
            "eV": 1.602176634e-19,
            "e_charge": 1.602176634e-19,
            "eps0": 8.8541878128e-12,
            "mu0": 1.25663706212e-6,
            "atm": 101325,
            "ly": 9.4607304725808e15,
            "au": 1.495978707e11,
        }
    )
    return ns


_CALC_NS = _calc_namespace()


def _calc_node(node):
    if isinstance(node, ast.Expression):
        return _calc_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex, bool)):
            return node.value
        raise ToolError("invalid constant")
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.BitXor):  # ^ = power (calculator convention)
            return operator.pow(_calc_node(node.left), _calc_node(node.right))
        fn = _CALC_BINOPS.get(type(node.op))
        if not fn:
            raise ToolError(f"operator {type(node.op).__name__} not allowed")
        return fn(_calc_node(node.left), _calc_node(node.right))
    if isinstance(node, ast.UnaryOp):
        fn = _CALC_UNARY.get(type(node.op))
        if not fn:
            raise ToolError("unary operator not allowed")
        return fn(_calc_node(node.operand))
    if isinstance(node, ast.BoolOp):
        vals = [_calc_node(v) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.Compare):
        left = _calc_node(node.left)
        for op, comp in zip(node.ops, node.comparators):
            fn = _CALC_CMP.get(type(op))
            if not fn:
                raise ToolError("comparison operator not allowed")
            right = _calc_node(comp)
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _calc_node(node.body) if _calc_node(node.test) else _calc_node(node.orelse)
    if isinstance(node, ast.Name):
        if node.id in _CALC_NS:
            return _CALC_NS[node.id]
        raise ToolError(f"unknown name: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ToolError("only named function calls are allowed")
        fn = _CALC_NS.get(node.func.id)
        if not callable(fn):
            raise ToolError(f"unknown function: {node.func.id}")
        args = [_calc_node(a) for a in node.args]
        kwargs = {k.arg: _calc_node(k.value) for k in node.keywords if k.arg}
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{node.func.id}() failed: {e}")
    if isinstance(node, ast.Tuple):
        return tuple(_calc_node(e) for e in node.elts)
    if isinstance(node, ast.List):
        return [_calc_node(e) for e in node.elts]
    raise ToolError(f"disallowed syntax: {type(node).__name__}")


def _calc_eval(expr):
    tree = ast.parse(expr, mode="eval")
    return _calc_node(tree.body)


def _fmt_num(x):
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if math.isnan(x):
            return "nan"
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        if x.is_integer():
            return str(int(x))
        return repr(x)
    if isinstance(x, complex):
        return str(x)
    return str(x)


def tool_calculate(args):
    op = (args.get("operation") or "eval").strip()
    if op == "eval":
        expr = args.get("expression")
        if not isinstance(expr, str) or not expr.strip():
            raise ToolError("expression is required (string)")
        result = _calc_eval(expr)
        return {"expression": expr, "result": _fmt_num(result)}
    if op == "base_convert":
        value = args.get("value")
        if value is None:
            raise ToolError("value is required")
        from_base = int(args.get("from_base", 10))
        to_base = int(args.get("to_base", 16))
        if not (2 <= to_base <= 36) or not (2 <= from_base <= 36):
            raise ToolError("bases must be between 2 and 36")
        if isinstance(value, str):
            n = int(value.strip(), from_base)
        else:
            n = int(value)
        if n == 0:
            res = "0"
        else:
            sign = "-" if n < 0 else ""
            n = abs(n)
            digs = "0123456789abcdefghijklmnopqrstuvwxyz"
            res = sign + ("".join(_base_digits(n, to_base, digs)))
        return {"decimal": n if sign == "" else -n, "result": res, "base": to_base}
    if op == "statistics":
        data = args.get("data")
        if not isinstance(data, list) or not data:
            raise ToolError("data must be a non-empty list of numbers")
        nums = [float(x) for x in data]
        sop = (args.get("stat") or args.get("op") or "mean").strip()
        fn = {
            "mean": statistics.mean,
            "median": statistics.median,
            "mode": statistics.mode,
            "stdev": statistics.stdev,
            "pstdev": statistics.pstdev,
            "variance": statistics.variance,
            "pvariance": statistics.pvariance,
            "min": min,
            "max": max,
            "sum": sum,
            "harmonic_mean": statistics.harmonic_mean,
            "geometric_mean": statistics.geometric_mean,
        }.get(sop)
        if not fn:
            raise ToolError(f"unknown stat: {sop}")
        if sop in ("stdev", "variance") and len(nums) < 2:
            raise ToolError("stdev/variance requires at least 2 values")
        if sop == "quantiles":
            q = float(args.get("q", 4))
            return {"op": "quantiles", "result": statistics.quantiles(nums, n=int(q))}
        return {"op": sop, "result": _fmt_num(fn(nums))}
    if op == "primes":
        pop = (args.get("prime_op") or args.get("op") or "is_prime").strip()
        if pop == "is_prime":
            return {"n": int(args["n"]), "is_prime": _is_prime(int(args["n"]))}
        if pop == "prime_factors":
            return {"n": int(args["n"]), "factors": _prime_factors(int(args["n"]))}
        if pop == "list_primes":
            count = int(args.get("count", 10))
            primes = []
            cand = 2
            while len(primes) < count:
                if _is_prime(cand):
                    primes.append(cand)
                cand += 1
            return {"count": count, "primes": primes}
        if pop == "nth_prime":
            n = int(args["n"])
            if n < 1:
                raise ToolError("n must be >= 1")
            primes = []
            cand = 2
            while len(primes) < n:
                if _is_prime(cand):
                    primes.append(cand)
                cand += 1
            return {"n": n, "prime": primes[-1]}
        raise ToolError(f"unknown prime op: {pop}")
    if op == "fib":
        return {"n": int(args["n"]), "fibonacci": _fib(int(args["n"]))}
    raise ToolError(f"unknown operation: {op}")


def _base_digits(n, base, digs):
    out = []
    while n:
        n, r = divmod(n, base)
        out.append(digs[r])
    return "".join(reversed(out))


# ─── datetime ──────────────────────────────────────────────────────────────────

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y%m%d",
    "%b %d %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%d %B %Y",
)


def _zone(tz):
    if not tz or tz == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz)
    except Exception:
        raise ToolError(f"unknown timezone: {tz}")


def _parse_dt(s, tz=None):
    s = str(s).strip()
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), _zone(tz) if tz else timezone.utc)
    iso = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if tz:
                dt = dt.replace(tzinfo=_zone(tz))
            return dt
        except ValueError:
            continue
    raise ToolError(f"could not parse datetime: {s}")


def _humanize(seconds):
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    parts = []
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n = seconds // sec
        seconds %= sec
        if n:
            parts.append(f"{n}{unit}")
    if not parts:
        parts.append("0s")
    return sign + " ".join(parts)


_DUR_RE = re.compile(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?\s*(?:(\d+)\s*ms)?", re.I)
_ISO_DUR_RE = re.compile(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", re.I)


def _parse_duration(s):
    s = str(s).strip()
    if ":" in s and not s.lower().startswith("p"):
        comps = [float(x) for x in s.split(":")]
        if len(comps) == 2:
            secs = comps[0] * 3600 + comps[1] * 60
        elif len(comps) == 3:
            secs = comps[0] * 3600 + comps[1] * 60 + comps[2]
        else:
            raise ToolError("colon duration needs H:MM:SS or H:MM")
        return int(round(secs))
    m = _ISO_DUR_RE.fullmatch(s)
    if m and s.upper().startswith("P"):
        y, mo, d, h, mi, sec = (float(x or 0) for x in m.groups())
        secs = y * 365.25 * 86400 + mo * 30.4375 * 86400 + d * 86400 + h * 3600 + mi * 60 + sec
        return int(round(secs))
    m = _DUR_RE.fullmatch(s)
    if not m or not any(m.groups()):
        raise ToolError(f"could not parse duration: {s}")
    d, h, mi, sec, ms = (float(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec + ms / 1000


def tool_datetime(args):
    op = (args.get("operation") or "now").strip()
    if op == "now":
        tz = args.get("timezone", "UTC")
        dt = datetime.now(_zone(tz))
        return {"iso": dt.isoformat(), "unix": dt.timestamp(), "timezone": tz}
    if op == "convert":
        dt = _parse_dt(args["datetime"], args.get("from_tz"))
        to = _zone(args["to_tz"])
        res = dt.astimezone(to)
        return {"input": dt.isoformat(), "result": res.isoformat(), "to_tz": args["to_tz"]}
    if op == "parse":
        dt = _parse_dt(args["datetime"], args.get("timezone"))
        return {
            "iso": dt.isoformat(),
            "unix": dt.timestamp(),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "tz": str(dt.tzinfo),
        }
    if op == "format":
        dt = _parse_dt(args["datetime"], args.get("timezone"))
        fmt = args.get("format", "%Y-%m-%d %H:%M:%S %Z")
        return {"formatted": dt.strftime(fmt)}
    if op == "to_unix":
        dt = _parse_dt(args["datetime"], args.get("timezone"))
        return {"unix": dt.timestamp()}
    if op == "from_unix":
        ts = float(args["timestamp"])
        tz = args.get("timezone", "UTC")
        dt = datetime.fromtimestamp(ts, _zone(tz))
        return {"iso": dt.isoformat(), "unix": ts, "timezone": tz}
    if op == "add" or op == "subtract":
        dt = _parse_dt(args["datetime"], args.get("timezone"))
        if "unit" in args:
            unit = args["unit"].lower()
            mult = {
                "s": 1,
                "sec": 1,
                "second": 1,
                "m": 60,
                "min": 60,
                "h": 3600,
                "hour": 3600,
                "d": 86400,
                "day": 86400,
                "w": 604800,
                "week": 604800,
            }.get(unit)
            if mult is None:
                raise ToolError(f"unknown unit: {unit}")
            secs = float(args["value"]) * mult
        else:
            secs = _parse_duration(str(args["value"]))
        if op == "subtract":
            secs = -secs
        res = dt + timedelta(seconds=secs)
        return {"result": res.isoformat()}
    if op == "business_days":
        start = _parse_dt(args["start"]).date()
        end = _parse_dt(args["end"]).date()
        weekend = [int(x) % 7 for x in (args.get("weekend") or [5, 6])]
        if start > end:
            start, end = end, start
        days = []
        cur = start
        while cur <= end:
            if cur.weekday() not in weekend:
                days.append(cur.isoformat())
            cur += timedelta(days=1)
        return {"start": start.isoformat(), "end": end.isoformat(), "count": len(days), "business_days": days}
    if op == "duration_parse":
        secs = _parse_duration(args["duration"])
        return {"seconds": secs, "human": _humanize(secs)}
    if op == "duration_humanize":
        secs = float(args["seconds"])
        return {"human": _humanize(secs)}
    if op == "calendar_info":
        year = int(args["year"])
        month = int(args["month"])
        import calendar

        return {
            "year": year,
            "month": month,
            "days_in_month": calendar.monthrange(year, month)[1],
            "first_weekday": calendar.monthrange(year, month)[0],
            "month_name": calendar.month_name[month],
        }
    raise ToolError(f"unknown operation: {op}")


# ─── text ──────────────────────────────────────────────────────────────────────

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    pad = 0
    for c in b:
        if c == 0:
            pad += 1
        else:
            break
    return _B58[0] * pad + s


def _b58decode(s):
    n = 0
    for c in s:
        idx = _B58.find(c)
        if idx < 0:
            raise ToolError(f"invalid base58 char: {c}")
        n = n * 58 + idx
    pad = 0
    for c in s:
        if c == _B58[0]:
            pad += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def _slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    return re.sub(r"[-\s]+", "-", s)


def _words(s):
    """Split a string into words on spaces, underscores, hyphens, and camelCase boundaries."""
    s = re.sub(r"[\s_\-]+", " ", s)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return [w for w in s.split() if w]


def _case(s, target):
    words = _words(s)
    if target == "upper":
        return s.upper()
    if target == "lower":
        return s.lower()
    if target == "title":
        return " ".join(w.capitalize() for w in words)
    if target == "camel":
        if not words:
            return ""
        return words[0].lower() + "".join(w.capitalize() for w in words[1:])
    if target == "pascal":
        return "".join(w.capitalize() for w in words)
    if target == "snake":
        return "_".join(w.lower() for w in words)
    if target == "kebab":
        return "-".join(w.lower() for w in words)
    if target == "slug":
        return "-".join(w.lower() for w in words)
    raise ToolError(f"unknown case: {target}")


def _levenshtein(a, b):
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def tool_text(args):
    op = (args.get("operation") or "").strip()
    if not op:
        raise ToolError("operation is required")
    if op == "case":
        return {"result": _case(args.get("text", ""), args.get("to"))}
    if op == "slugify":
        return {"result": _slugify(args.get("text", ""))}
    if op == "reverse":
        return {"result": args.get("text", "")[::-1]}
    if op == "truncate":
        text = args.get("text", "")
        n = int(args.get("length", 100))
        suffix = args.get("suffix", "...")
        return {"result": text if len(text) <= n else text[:n] + suffix}
    if op == "pad":
        text = args.get("text", "")
        n = int(args.get("length", 0))
        side = args.get("side", "right")
        ch = args.get("char", " ")[:1] or " "
        if side == "left":
            return {"result": text.rjust(n, ch)}
        if side == "center":
            return {"result": text.center(n, ch)}
        return {"result": text.ljust(n, ch)}
    if op == "count":
        text = args.get("text", "")
        return {"chars": len(text), "words": len(text.split()), "lines": text.count("\n") + 1}
    if op == "sort_lines":
        lines = args.get("text", "").split("\n")
        reverse = bool(args.get("reverse", False))
        return {"result": "\n".join(sorted(lines, reverse=reverse))}
    if op == "dedupe":
        lines = args.get("text", "").split("\n")
        seen = set()
        out = []
        for ln in lines:
            if ln not in seen:
                seen.add(ln)
                out.append(ln)
        return {"result": "\n".join(out)}
    if op == "encode":
        text = args.get("text", "")
        enc = args.get("encoding", "base64")
        if enc == "base64":
            return {"result": base64.b64encode(text.encode()).decode()}
        if enc == "base32":
            return {"result": base64.b32encode(text.encode()).decode()}
        if enc == "base58":
            return {"result": _b58encode(text.encode())}
        if enc == "hex":
            return {"result": binascii.hexlify(text.encode()).decode()}
        if enc == "url":
            return {"result": urllib.parse.quote(text, safe=args.get("safe", ""))}
        if enc == "rot13":
            return {"result": codecs_rot13(text)}
        raise ToolError(f"unknown encoding: {enc}")
    if op == "decode":
        text = args.get("text", "")
        enc = args.get("encoding", "base64")
        try:
            if enc == "base64":
                return {"result": base64.b64decode(text).decode("utf-8", "replace")}
            if enc == "base32":
                return {"result": base64.b32decode(text).decode("utf-8", "replace")}
            if enc == "base58":
                return {"result": _b58decode(text).decode("utf-8", "replace")}
            if enc == "hex":
                return {"result": binascii.unhexlify(text).decode("utf-8", "replace")}
            if enc == "url":
                return {"result": urllib.parse.unquote(text)}
            if enc == "rot13":
                return {"result": codecs_rot13(text)}
        except Exception as e:
            raise ToolError(f"decode failed: {e}")
        raise ToolError(f"unknown encoding: {enc}")
    if op == "hash":
        text = args.get("text", "")
        algo = (args.get("algorithm") or "sha256").lower()
        data = text.encode("utf-8")
        if algo in hashlib.algorithms_available:
            if args.get("key"):
                return {"result": hmac.new(str(args["key"]).encode(), data, algo).hexdigest()}
            return {"result": hashlib.new(algo, data).hexdigest()}
        raise ToolError(f"unknown algorithm: {algo}")
    if op == "regex":
        pattern = args.get("pattern")
        text = args.get("text", "")
        rop = (args.get("regex_op") or "test").strip()
        if not pattern:
            raise ToolError("pattern is required")
        flags = 0
        if args.get("ignore_case"):
            flags |= re.IGNORECASE
        if args.get("multiline"):
            flags |= re.MULTILINE
        if rop == "test":
            return {"match": bool(re.search(pattern, text, flags))}
        if rop == "match":
            m = re.search(pattern, text, flags)
            if not m:
                return {"match": None}
            return {"match": m.group(0), "groups": list(m.groups())}
        if rop == "extract":
            return {"matches": [m.group(0) for m in re.finditer(pattern, text, flags)]}
        if rop == "replace":
            return {"result": re.sub(pattern, str(args.get("replacement", "")), text, flags=flags)}
        raise ToolError(f"unknown regex op: {rop}")
    if op == "diff":
        a = args.get("text_a", "").split("\n")
        b = args.get("text_b", "").split("\n")
        return {"diff": "\n".join(difflib.unified_diff(a, b, fromfile="a", tofile="b", lineterm=""))}
    if op == "json":
        text = args.get("text", "")
        jop = (args.get("json_op") or "pretty").strip()
        if jop == "validate":
            try:
                json.loads(text)
                return {"valid": True}
            except Exception as e:
                return {"valid": False, "error": str(e)}
        if jop == "pretty":
            return {"result": json.dumps(json.loads(text), indent=2, ensure_ascii=False)}
        if jop == "minify":
            return {"result": json.dumps(json.loads(text), separators=(",", ":"))}
        raise ToolError(f"unknown json op: {jop}")
    if op == "jwt_decode":
        token = args.get("text", "")
        parts = token.split(".")
        if len(parts) < 2:
            raise ToolError("not a JWT (needs 2+ segments)")
        out = {}
        for i, name in enumerate(("header", "payload")):
            seg = parts[i] + "=" * (-len(parts[i]) % 4)
            try:
                out[name] = json.loads(base64.urlsafe_b64decode(seg))
            except Exception:
                out[name] = "(unparseable)"
        return out
    if op == "levenshtein":
        return {"distance": _levenshtein(args.get("text_a", ""), args.get("text_b", ""))}
    if op == "lorem":
        count = int(args.get("count", 1))
        words = (
            "lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
            "eiusmod tempor incididunt ut labore et dolore magna aliqua ut enim "
            "ad minim veniam quis nostrud exercitation ullamco laboris nisi"
        ).split()
        out = []
        for _ in range(count):
            out.append(" ".join(_random.choice(words) for _ in range(8)))
        return {"result": "\n".join(out)}
    if op == "wrap":
        text = args.get("text", "")
        width = int(args.get("width", 80))
        return {"result": "\n".join(_chunk(text, width))}
    raise ToolError(f"unknown operation: {op}")


def codecs_rot13(s):
    import codecs

    try:
        return codecs.encode(s, "rot_13")
    except Exception:
        return s.translate(
            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm")
        )


def _chunk(text, width):
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        out.append(line)
    return out


# ─── generate_id ───────────────────────────────────────────────────────────────

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_NANO_ALPHABET = "_-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _ulid():
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    raw = ms.to_bytes(6, "big") + rand
    n = int.from_bytes(raw, "big")
    s = ""
    for _ in range(26):
        n, r = divmod(n, 32)
        s = _ULID_ALPHABET[r] + s
    return s


def _password(length, sets):
    pool = ""
    if "lower" in sets:
        pool += _string.ascii_lowercase
    if "upper" in sets:
        pool += _string.ascii_uppercase
    if "digits" in sets:
        pool += _string.digits
    if "symbols" in sets:
        pool += "!@#$%^&*()-_=+[]{};:,.?/"
    if not pool:
        pool = _string.ascii_letters + _string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


def tool_generate_id(args):
    op = (args.get("operation") or "uuid").strip()
    if op == "uuid":
        ver = int(args.get("version", 4))
        if ver == 1:
            return {"uuid": str(_uuid.uuid1())}
        if ver == 3:
            ns = args.get("namespace", "dns")
            ns_map = {"dns": _uuid.NAMESPACE_DNS, "url": _uuid.NAMESPACE_URL, "oid": _uuid.NAMESPACE_OID, "x500": _uuid.NAMESPACE_X500}
            return {"uuid": str(_uuid.uuid3(ns_map.get(ns, _uuid.NAMESPACE_DNS), args["name"]))}
        if ver == 4:
            return {"uuid": str(_uuid.uuid4())}
        if ver == 5:
            ns = args.get("namespace", "dns")
            ns_map = {"dns": _uuid.NAMESPACE_DNS, "url": _uuid.NAMESPACE_URL, "oid": _uuid.NAMESPACE_OID, "x500": _uuid.NAMESPACE_X500}
            return {"uuid": str(_uuid.uuid5(ns_map.get(ns, _uuid.NAMESPACE_DNS), args["name"]))}
        raise ToolError(f"unsupported uuid version: {ver}")
    if op == "ulid":
        return {"ulid": _ulid()}
    if op == "nanoid":
        size = int(args.get("size", 21))
        alphabet = args.get("alphabet") or _NANO_ALPHABET
        return {"nanoid": "".join(secrets.choice(alphabet) for _ in range(size))}
    if op == "token":
        length = int(args.get("length", 32))
        kind = (args.get("kind") or "urlsafe").strip()
        if kind == "hex":
            return {"token": secrets.token_hex(length)}
        if kind == "bytes":
            return {"token": secrets.token_bytes(length).hex()}
        if kind == "alnum":
            pool = _string.ascii_letters + _string.digits
            return {"token": "".join(secrets.choice(pool) for _ in range(length))}
        return {"token": secrets.token_urlsafe(length)}
    if op == "password":
        length = int(args.get("length", 16))
        sets = args.get("sets") or ["lower", "upper", "digits", "symbols"]
        if isinstance(sets, str):
            sets = [s.strip() for s in sets.split(",")]
        pw = _password(length, sets)
        entropy = len(pw) * math.log2(max(len(set(pw)), 1)) if pw else 0
        return {"password": pw, "approx_entropy_bits": round(entropy, 1)}
    if op == "short_id":
        length = int(args.get("length", 8))
        return {"id": "".join(secrets.choice(_string.ascii_lowercase + _string.digits) for _ in range(length))}
    if op == "random_choice":
        items = args.get("items", [])
        return {"choice": _random.choice(items)}
    if op == "random_sample":
        items = args.get("items", [])
        k = int(args.get("count", 1))
        return {"sample": _random.sample(items, min(k, len(items)))}
    if op == "shuffle":
        items = list(args.get("items", []))
        _random.shuffle(items)
        return {"shuffled": items}
    raise ToolError(f"unknown operation: {op}")


# ─── convert_units ─────────────────────────────────────────────────────────────

_UNITS = {
    "length": {
        "m": 1,
        "km": 1000,
        "cm": 0.01,
        "mm": 0.001,
        "mi": 1609.344,
        "yd": 0.9144,
        "ft": 0.3048,
        "in": 0.0254,
        "nmi": 1852,
        "ly": 9.4607304725808e15,
        "au": 1.495978707e11,
    },
    "mass": {
        "kg": 1,
        "g": 0.001,
        "mg": 1e-6,
        "t": 1000,
        "lb": 0.45359237,
        "oz": 0.028349523125,
        "st": 6.35029318,
        "ton_us": 907.18474,
        "ton_uk": 1016.0469088,
    },
    "data": {
        "b": 1,
        "byte": 1,
        "bit": 0.125,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
        "pb": 1024**5,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    },
    "speed": {"mps": 1, "kph": 0.277777778, "mph": 0.44704, "fps": 0.3048, "knot": 0.514444444, "mach": 343},
    "angle": {"deg": 1, "rad": 57.29577951308232, "grad": 0.9, "arcmin": 1 / 60, "arcsec": 1 / 3600},
    "pressure": {"pa": 1, "kpa": 1000, "bar": 100000, "atm": 101325, "psi": 6894.757293, "torr": 133.322368421, "mmhg": 133.322368421},
    "volume": {
        "l": 1,
        "ml": 0.001,
        "m3": 1000,
        "cm3": 0.001,
        "gal_us": 3.785411784,
        "gal_uk": 4.54609,
        "qt_us": 0.946352946,
        "pt_us": 0.473176473,
        "floz_us": 0.0295735296,
        "cup": 0.2365882365,
    },
    "area": {
        "m2": 1,
        "km2": 1e6,
        "cm2": 1e-4,
        "ha": 10000,
        "acre": 4046.8564224,
        "ft2": 0.09290304,
        "in2": 0.00064516,
        "mi2": 2589988.110336,
    },
    "time": {"s": 1, "ms": 0.001, "us": 1e-6, "min": 60, "h": 3600, "d": 86400, "w": 604800, "year": 31557600},
    "fuel": {"mpg_us": 1, "l_100km": -1, "km_l": 1},
}


def _convert_temp(value, frm, to):
    if frm == to:
        return value
    if frm == "c":
        kel = value + 273.15
    elif frm == "f":
        kel = (value - 32) * 5 / 9 + 273.15
    elif frm == "k":
        kel = value
    elif frm == "rankine":
        kel = value * 5 / 9
    else:
        raise ToolError(f"unknown temperature unit: {frm}")
    if to == "c":
        return kel - 273.15
    if to == "f":
        return (kel - 273.15) * 9 / 5 + 32
    if to == "k":
        return kel
    if to == "rankine":
        return kel * 9 / 5
    raise ToolError(f"unknown temperature unit: {to}")


def tool_convert_units(args):
    category = (args.get("category") or "").strip()
    value = float(args["value"])
    frm = (args.get("from_unit") or "").strip()
    to = (args.get("to_unit") or "").strip()
    if not frm or not to:
        raise ToolError("from_unit and to_unit are required")
    if category == "temperature":
        result = _convert_temp(value, frm.lower(), to.lower())
        return {"value": value, "from": frm, "to": to, "result": _fmt_num(result)}
    if category == "fuel":
        if frm == to:
            return {"result": value}
        if frm == "l_100km" and to == "mpg_us":
            return {"result": _fmt_num(235.214583 / value if value else 0)}
        if frm == "mpg_us" and to == "l_100km":
            return {"result": _fmt_num(235.214583 / value if value else 0)}
        if frm == "km_l" and to == "l_100km":
            return {"result": _fmt_num(100 / value if value else 0)}
        if frm == "km_l" and to == "mpg_us":
            return {"result": _fmt_num(value * 2.352145833)}
        if frm == "mpg_us" and to == "km_l":
            return {"result": _fmt_num(value / 2.352145833)}
        raise ToolError(f"unsupported fuel conversion: {frm} -> {to}")
    table = _UNITS.get(category)
    if not table:
        raise ToolError(f"unknown category: {category} (try one of: {', '.join(_UNITS)})")
    if frm not in table or to not in table:
        raise ToolError(f"units must be one of: {', '.join(sorted(table))}")
    base = value * table[frm]
    result = base / table[to]
    return {"value": value, "from": frm, "to": to, "category": category, "result": _fmt_num(result)}


# ─── semver ────────────────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def _semver_parse(s):
    m = _SEMVER_RE.match(str(s).strip())
    if not m:
        raise ToolError(f"invalid semver: {s}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4)
    pre_parts = pre.split(".") if pre else []
    return (major, minor, patch, pre_parts)


def _pre_cmp(a, b):
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for x, y in zip(a, b):
        xi = x.isdigit()
        yi = y.isdigit()
        if xi and yi:
            xn, yn = int(x), int(y)
            if xn != yn:
                return -1 if xn < yn else 1
        elif xi and not yi:
            return -1
        elif yi and not xi:
            return 1
        else:
            if x != y:
                return -1 if x < y else 1
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return 0


def _semver_cmp(a, b):
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    return _pre_cmp(a[3], b[3])


def _semver_satisfies(v, rng):
    rng = rng.strip()
    if rng in ("", "*"):
        return True
    vparts = _semver_parse(v)
    for alt in rng.split("||"):
        alt = alt.strip()
        ok = True
        for clause in alt.split():
            clause = clause.strip()
            if not clause:
                continue
            m = re.match(r"^(\^|~|>=|<=|>|<|=)?v?(.+)$", clause)
            op, rest = m.group(1) or "=", m.group(2)
            cv = _semver_parse(rest)
            if op == "=":
                if _semver_cmp(vparts, cv) != 0:
                    ok = False
            elif op == ">":
                if _semver_cmp(vparts, cv) <= 0:
                    ok = False
            elif op == "<":
                if _semver_cmp(vparts, cv) >= 0:
                    ok = False
            elif op == ">=":
                if _semver_cmp(vparts, cv) < 0:
                    ok = False
            elif op == "<=":
                if _semver_cmp(vparts, cv) > 0:
                    ok = False
            elif op == "~":  # ~M.m.p := >=M.m.p <M.(m+1).0
                if vparts[0] != cv[0] or vparts[1] != cv[1] or _semver_cmp(vparts, cv) < 0:
                    ok = False
            elif op == "^":  # ^M.m.p := >=M.m.p, same leftmost-non-zero
                M, m = cv[0], cv[1]
                if _semver_cmp(vparts, cv) < 0:
                    ok = False
                elif M > 0:
                    if vparts[0] != M:
                        ok = False
                elif m > 0:
                    if vparts[0] != 0 or vparts[1] != m:
                        ok = False
                else:
                    if vparts[0] != 0 or vparts[1] != 0 or vparts[2] != cv[2]:
                        ok = False
        if ok:
            return True
    return False


def _bump(v, kind):
    major, minor, patch, pre = _semver_parse(v)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if kind == "pre":
        return f"{major}.{minor}.{patch}-beta.0"
    raise ToolError(f"unknown bump kind: {kind}")


def tool_semver(args):
    op = (args.get("operation") or "").strip()
    if not op:
        raise ToolError("operation is required")
    if op == "parse":
        p = _semver_parse(args["version"])
        return {"major": p[0], "minor": p[1], "patch": p[2], "prerelease": ".".join(p[3]) or None}
    if op == "valid":
        return {"valid": bool(_SEMVER_RE.match(str(args["version"]).strip()))}
    if op in ("compare", "gt", "lt", "eq", "gte", "lte"):
        a = _semver_parse(args["a"])
        b = _semver_parse(args["b"])
        c = _semver_cmp(a, b)
        return {"compare": c, "gt": c > 0, "lt": c < 0, "eq": c == 0, "gte": c >= 0, "lte": c <= 0}
    if op == "bump":
        return {"result": _bump(args["version"], args.get("kind", "patch"))}
    if op == "satisfies":
        return {"satisfies": _semver_satisfies(args["version"], args.get("range", "*"))}
    if op == "max_satisfying":
        rng = args.get("range", "*")
        versions = args.get("versions", [])
        best = None
        for v in versions:
            if _semver_satisfies(v, rng):
                if best is None or _semver_cmp(_semver_parse(v), _semver_parse(best)) > 0:
                    best = v
        return {"max_satisfying": best}
    if op == "sort":
        versions = args.get("versions", [])
        return {"sorted": sorted(versions, key=_semver_parse)}
    raise ToolError(f"unknown operation: {op}")


# ─── network ───────────────────────────────────────────────────────────────────


def tool_network(args):
    op = (args.get("operation") or "").strip()
    if not op:
        raise ToolError("operation is required")
    if op == "ip_parse":
        try:
            ip = ipaddress.ip_address(args["address"])
        except ValueError as e:
            raise ToolError(str(e))
        return {
            "address": str(ip),
            "version": ip.version,
            "is_private": ip.is_private,
            "is_loopback": ip.is_loopback,
            "is_multicast": ip.is_multicast,
            "is_link_local": ip.is_link_local,
            "is_reserved": ip.is_reserved,
        }
    if op == "cidr_info":
        try:
            net = ipaddress.ip_network(args["cidr"], strict=False)
        except ValueError as e:
            raise ToolError(str(e))
        return {
            "network": str(net.network_address),
            "prefix": net.prefixlen,
            "netmask": str(net.netmask),
            "broadcast": str(net.broadcast_address),
            "num_addresses": net.num_addresses,
            "version": net.version,
            "is_private": net.is_private,
            "first_host": str(net.network_address + 1) if net.num_addresses > 2 else str(net.network_address),
            "last_host": str(net.broadcast_address - 1) if net.num_addresses > 2 else str(net.broadcast_address),
        }
    if op == "ip_in_cidr":
        try:
            ip = ipaddress.ip_address(args["address"])
            net = ipaddress.ip_network(args["cidr"], strict=False)
        except ValueError as e:
            raise ToolError(str(e))
        return {"in_cidr": ip in net, "address": str(ip), "cidr": str(net)}
    if op == "url_parse":
        p = urllib.parse.urlparse(args["url"])
        q = urllib.parse.parse_qs(p.query)
        params = {k: (v[0] if len(v) == 1 else v) for k, v in q.items()}
        return {
            "scheme": p.scheme,
            "netloc": p.netloc,
            "host": p.hostname,
            "port": p.port,
            "path": p.path,
            "query": p.query,
            "fragment": p.fragment,
            "params": params,
        }
    if op == "url_resolve":
        return {"result": urllib.parse.urljoin(args["base"], args["ref"])}
    if op == "url_build":
        return {
            "result": urllib.parse.urlunparse(
                (
                    args.get("scheme", ""),
                    args.get("netloc", ""),
                    args.get("path", ""),
                    args.get("params", ""),
                    args.get("query", ""),
                    args.get("fragment", ""),
                )
            )
        }
    if op == "url_encode":
        return {"result": urllib.parse.quote(args.get("text", ""), safe=args.get("safe", ""))}
    if op == "url_decode":
        return {"result": urllib.parse.unquote(args.get("text", ""))}
    if op == "query_parse":
        qs = args.get("query", "")
        if not qs and "url" in args:
            qs = urllib.parse.urlparse(args["url"]).query
        return {"params": {k: v[0] if len(v) == 1 else v for k, v in urllib.parse.parse_qs(qs).items()}}
    raise ToolError(f"unknown operation: {op}")


# ─── color ─────────────────────────────────────────────────────────────────────


def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ToolError("hex must be #rgb or #rrggbb")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _clamp8(c):
    return max(0, min(255, int(round(c))))


def _rgb_to_hex(r, g, b):
    return f"#{_clamp8(r):02x}{_clamp8(g):02x}{_clamp8(b):02x}"


def _rgb_to_hsl(r, g, b):
    r, g, b = r / 255, g / 255, b / 255
    mx, mn = max(r, g, b), min(r, g, b)
    light = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, light
    d = mx - mn
    s = d / (2 - mx - mn) if light > 0.5 else d / (mx + mn)
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60, s, light


def _hsl_to_rgb(h, s, light):
    h = h % 360 / 360
    if s == 0:
        v = light * 255
        return v, v, v

    def hue(p, q, t):
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = light * (1 + s) if light < 0.5 else light + s - light * s
    p = 2 * light - q
    r = hue(p, q, h + 1 / 3) * 255
    g = hue(p, q, h) * 255
    b = hue(p, q, h - 1 / 3) * 255
    return r, g, b


def _luminance(r, g, b):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def tool_color(args):
    op = (args.get("operation") or "").strip()
    if not op:
        raise ToolError("operation is required")
    if op == "hex_to_rgb":
        r, g, b = _hex_to_rgb(args["hex"])
        return {"r": r, "g": g, "b": b, "rgb": f"rgb({r}, {g}, {b})"}
    if op == "rgb_to_hex":
        return {"hex": _rgb_to_hex(args["r"], args["g"], args["b"])}
    if op == "rgb_to_hsl":
        r, g, b = int(args["r"]), int(args["g"]), int(args["b"])
        h, s, light = _rgb_to_hsl(r, g, b)
        return {
            "h": round(h, 1),
            "s": round(s * 100, 1),
            "l": round(light * 100, 1),
            "hsl": f"hsl({round(h)}, {round(s * 100)}%, {round(light * 100)}%)",
        }
    if op == "hsl_to_rgb":
        r, g, b = _hsl_to_rgb(float(args["h"]), float(args["s"]) / 100, float(args["l"]) / 100)
        return {"r": round(r), "g": round(g), "b": round(b), "hex": _rgb_to_hex(r, g, b), "rgb": f"rgb({round(r)}, {round(g)}, {round(b)})"}
    if op == "luminance":
        r, g, b = int(args["r"]), int(args["g"]), int(args["b"])
        return {"luminance": round(_luminance(r, g, b), 4)}
    if op == "contrast_ratio":
        c1 = (int(args["r1"]), int(args["g1"]), int(args["b1"]))
        c2 = (int(args["r2"]), int(args["g2"]), int(args["b2"]))
        l1, l2 = _luminance(*c1), _luminance(*c2)
        hi, lo = max(l1, l2), min(l1, l2)
        return {"ratio": round((hi + 0.05) / (lo + 0.05), 2)}
    if op == "complementary":
        r, g, b = _hex_to_rgb(args["hex"])
        h, s, light = _rgb_to_hsl(r, g, b)
        r2, g2, b2 = _hsl_to_rgb((h + 180) % 360, s, light)
        return {"hex": _rgb_to_rgb_hex(r2, g2, b2)}
    if op == "mix":
        c1 = _hex_to_rgb(args["hex1"])
        c2 = _hex_to_rgb(args["hex2"])
        ratio = float(args.get("ratio", 0.5))
        mixed = tuple(round(a * (1 - ratio) + b * ratio) for a, b in zip(c1, c2))
        return {"hex": _rgb_to_hex(*mixed), "rgb": f"rgb{mixed}"}
    if op == "lighten" or op == "darken":
        r, g, b = _hex_to_rgb(args["hex"])
        amt = float(args.get("amount", 0.1))
        target = (255, 255, 255) if op == "lighten" else (0, 0, 0)
        mixed = tuple(round(a * (1 - amt) + t * amt) for a, t in zip((r, g, b), target))
        return {"hex": _rgb_to_hex(*mixed)}
    if op == "random":
        r, g, b = secrets.randbelow(256), secrets.randbelow(256), secrets.randbelow(256)
        h, s, light = _rgb_to_hsl(r, g, b)
        return {
            "hex": _rgb_to_hex(r, g, b),
            "rgb": f"rgb({r}, {g}, {b})",
            "hsl": f"hsl({round(h)}, {round(s * 100)}%, {round(light * 100)}%)",
        }
    raise ToolError(f"unknown operation: {op}")


def _rgb_to_rgb_hex(r, g, b):
    return _rgb_to_hex(r, g, b)


# ─── tool registry ─────────────────────────────────────────────────────────────


def _obj(properties, required=None, description=None):
    s = {"type": "object", "properties": properties}
    if required:
        s["required"] = required
    if description:
        s["description"] = description
    return s


TOOL_DEFS = [
    {
        "name": "calculate",
        "description": (
            "Mathematical calculations. operation=eval: evaluate a math expression safely "
            "(full math/cmath/statistics functions, constants pi/e/tau/phi/g/c/h/k/Na/R, "
            "** or ^ for power, fib(n), is_prime(n), prime_factors(n), bit_and/or/xor/shl/shr, "
            "gcd/lcm/comb/perm/factorial). operation=base_convert (value, from_base, to_base). "
            "operation=statistics (data list, stat: mean/median/mode/stdev/pstdev/variance/min/max/sum/quantiles). "
            "operation=primes (prime_op: is_prime/prime_factors/list_primes/nth_prime). operation=fib (n)."
        ),
        "inputSchema": _obj(
            {
                "operation": {
                    "type": "string",
                    "enum": ["eval", "base_convert", "statistics", "primes", "fib"],
                    "description": "Calculation to perform.",
                    "default": "eval",
                },
                "expression": {"type": "string", "description": "Math expression (operation=eval)."},
                "value": {"description": "Number or string (base_convert)."},
                "from_base": {"type": "integer", "description": "Parse base (base_convert), default 10.", "default": 10},
                "to_base": {"type": "integer", "description": "Output base (base_convert), default 16.", "default": 16},
                "data": {"type": "array", "items": {"type": "number"}, "description": "Numbers (statistics)."},
                "stat": {"type": "string", "description": "Statistic (statistics)."},
                "prime_op": {"type": "string", "description": "is_prime|prime_factors|list_primes|nth_prime (primes)."},
                "count": {"type": "integer", "description": "Count (list_primes) or sample size."},
                "n": {"type": "integer", "description": "Integer argument (primes, fib)."},
                "q": {"type": "number", "description": "Quantile count (statistics, stat=quantiles)."},
            },
            ["operation"],
        ),
        "fn": tool_calculate,
    },
    {
        "name": "datetime",
        "description": (
            "Date/time in any IANA timezone. operation: now, convert (datetime, from_tz, to_tz), "
            "parse, format (strftime), to_unix, from_unix, add, subtract (value + unit s/m/h/d/w), "
            "business_days (start, end, weekend weekdays), duration_parse, duration_humanize, calendar_info (year, month)."
        ),
        "inputSchema": _obj(
            {
                "operation": {
                    "type": "string",
                    "enum": [
                        "now",
                        "convert",
                        "parse",
                        "format",
                        "to_unix",
                        "from_unix",
                        "add",
                        "subtract",
                        "business_days",
                        "duration_parse",
                        "duration_humanize",
                        "calendar_info",
                    ],
                },
                "datetime": {"type": "string", "description": "ISO/date/timestamp string."},
                "timezone": {"type": "string", "description": "IANA tz e.g. America/New_York (default UTC)."},
                "from_tz": {"type": "string"},
                "to_tz": {"type": "string"},
                "format": {"type": "string", "description": "strftime format."},
                "timestamp": {"type": "number", "description": "Unix timestamp (from_unix)."},
                "value": {"description": "Amount (add/subtract) or seconds (duration_humanize) or year."},
                "unit": {"type": "string", "description": "s|m|h|d|w (add/subtract)."},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "weekend": {"type": "array", "items": {"type": "integer"}, "description": "Weekday ints 0=Mon..6=Sun (default [5,6])."},
                "duration": {"type": "string", "description": "e.g. 2h30m, 1:30:00, P1DT2H."},
                "year": {"type": "integer"},
                "month": {"type": "integer"},
            },
            ["operation"],
        ),
        "fn": tool_datetime,
    },
    {
        "name": "text",
        "description": (
            "String & encoding ops. operation: case (to: upper/lower/title/camel/pascal/snake/kebab/slug), "
            "slugify, reverse, truncate, pad, count, sort_lines, dedupe, encode, decode "
            "(base64/base32/base58/hex/url/rot13), hash (algorithm, optional key for HMAC), "
            "regex (regex_op: test/match/extract/replace), diff, json (json_op: pretty/minify/validate), "
            "jwt_decode, levenshtein, lorem, wrap."
        ),
        "inputSchema": _obj(
            {
                "operation": {"type": "string"},
                "text": {"type": "string"},
                "to": {"type": "string", "description": "target case (case)."},
                "length": {"type": "integer"},
                "encoding": {"type": "string", "description": "base64|base32|base58|hex|url|rot13."},
                "algorithm": {"type": "string", "description": "md5|sha1|sha256|sha512|...", "default": "sha256"},
                "key": {"type": "string", "description": "HMAC key (hash)."},
                "pattern": {"type": "string"},
                "replacement": {"type": "string"},
                "regex_op": {"type": "string"},
                "ignore_case": {"type": "boolean"},
                "multiline": {"type": "boolean"},
                "json_op": {"type": "string"},
                "text_a": {"type": "string"},
                "text_b": {"type": "string"},
                "count": {"type": "integer"},
                "width": {"type": "integer"},
                "side": {"type": "string"},
                "char": {"type": "string"},
                "safe": {"type": "string"},
            },
            ["operation"],
        ),
        "fn": tool_text,
    },
    {
        "name": "generate_id",
        "description": (
            "Generate identifiers & secrets. operation: uuid (version 1/3/4/5, name+namespace for 3/5), "
            "ulid (sortable 26-char), nanoid (size, alphabet), token (kind: hex/bytes/urlsafe/alnum, length), "
            "password (length, sets), short_id, random_choice, random_sample, shuffle."
        ),
        "inputSchema": _obj(
            {
                "operation": {"type": "string"},
                "version": {"type": "integer", "description": "UUID version (default 4).", "default": 4},
                "namespace": {"type": "string", "description": "dns|url|oid|x500 (uuid v3/v5)."},
                "name": {"type": "string", "description": "name (uuid v3/v5)."},
                "size": {"type": "integer", "description": "nanoid size (default 21).", "default": 21},
                "alphabet": {"type": "string", "description": "custom alphabet (nanoid)."},
                "length": {"type": "integer", "description": "token/password length."},
                "kind": {"type": "string", "description": "hex|bytes|urlsafe|alnum (token).", "default": "urlsafe"},
                "sets": {"description": "password char sets: lower/upper/digits/symbols (list or csv)."},
                "items": {"type": "array", "description": "items (random_choice/sample/shuffle)."},
                "count": {"type": "integer", "description": "sample count."},
            },
            ["operation"],
        ),
        "fn": tool_generate_id,
    },
    {
        "name": "convert_units",
        "description": (
            "Unit conversion. category: length, mass, temperature (c/f/k/rankine), data, speed, angle, "
            "pressure, volume, area, time, fuel. Provide value, from_unit, to_unit."
        ),
        "inputSchema": _obj(
            {
                "category": {"type": "string", "enum": list(_UNITS.keys()) + ["temperature", "fuel"]},
                "value": {"type": "number"},
                "from_unit": {"type": "string"},
                "to_unit": {"type": "string"},
            },
            ["category", "value", "from_unit", "to_unit"],
        ),
        "fn": tool_convert_units,
    },
    {
        "name": "semver",
        "description": (
            "Semantic versioning (semver.org). operation: parse, valid, compare/gt/lt/eq/gte/lte (a, b), "
            "bump (kind: major/minor/patch/pre), satisfies (version, range with >=,<,~,^,*,||), "
            "max_satisfying (versions, range), sort (versions)."
        ),
        "inputSchema": _obj(
            {
                "operation": {"type": "string"},
                "version": {"type": "string"},
                "a": {"type": "string"},
                "b": {"type": "string"},
                "kind": {"type": "string", "description": "major|minor|patch|pre (bump)."},
                "range": {"type": "string", "description": "e.g. >=1.2.0 <2.0.0"},
                "versions": {"type": "array", "items": {"type": "string"}},
            },
            ["operation"],
        ),
        "fn": tool_semver,
    },
    {
        "name": "network",
        "description": (
            "IP & URL utilities. operation: ip_parse (address), cidr_info (cidr), ip_in_cidr (address, cidr), "
            "url_parse (url), url_resolve (base, ref), url_build, url_encode/url_decode, query_parse (url or query)."
        ),
        "inputSchema": _obj(
            {
                "operation": {"type": "string"},
                "address": {"type": "string"},
                "cidr": {"type": "string"},
                "url": {"type": "string"},
                "base": {"type": "string"},
                "ref": {"type": "string"},
                "query": {"type": "string"},
                "text": {"type": "string"},
                "scheme": {"type": "string"},
                "netloc": {"type": "string"},
                "path": {"type": "string"},
                "params": {"type": "string"},
                "fragment": {"type": "string"},
                "safe": {"type": "string"},
            },
            ["operation"],
        ),
        "fn": tool_network,
    },
    {
        "name": "color",
        "description": (
            "Color conversions. operation: hex_to_rgb, rgb_to_hex, rgb_to_hsl, hsl_to_rgb, "
            "luminance (r,g,b), contrast_ratio (r1,g1,b1,r2,g2,b2), complementary (hex), "
            "mix (hex1, hex2, ratio), lighten/darken (hex, amount), random."
        ),
        "inputSchema": _obj(
            {
                "operation": {"type": "string"},
                "hex": {"type": "string"},
                "hex1": {"type": "string"},
                "hex2": {"type": "string"},
                "r": {"type": "integer"},
                "g": {"type": "integer"},
                "b": {"type": "integer"},
                "h": {"type": "number"},
                "s": {"type": "number"},
                "l": {"type": "number"},
                "r1": {"type": "integer"},
                "g1": {"type": "integer"},
                "b1": {"type": "integer"},
                "r2": {"type": "integer"},
                "g2": {"type": "integer"},
                "b2": {"type": "integer"},
                "ratio": {"type": "number"},
                "amount": {"type": "number"},
            },
            ["operation"],
        ),
        "fn": tool_color,
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOL_DEFS}


# ─── JSON-RPC / MCP dispatch ───────────────────────────────────────────────────


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        tools = [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in TOOL_DEFS]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True},
            }
        try:
            result = tool["fn"](arguments if isinstance(arguments, dict) else {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": _to_text(result)}}
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        except KeyError as e:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": f"missing required argument: {e}"}], "isError": True},
            }
        except Exception as e:
            _log(f"tool {name} error: {type(e).__name__}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True},
            }
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def main():
    _log("started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log(f"bad json: {line[:120]}")
            continue
        resp = handle(msg)
        if resp is not None:
            _send(resp)
    _log("stdin closed, exiting")


if __name__ == "__main__":
    main()
