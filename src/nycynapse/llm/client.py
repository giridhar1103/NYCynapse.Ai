"""Provider-agnostic model client.

Providers and the role each one plays (router, planner, sql, judge, ...) come from a JSON file
outside the repository, named by NYCYNAPSE_PROVIDERS. Three provider types are supported:

- anthropic: the Messages API over HTTPS, key from an environment variable
- openai: any OpenAI-compatible chat completions endpoint
- command: a local inference runtime run as a subprocess, prompt on stdin, reply on stdout
  (plain text or a JSON envelope with a `result` field)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

# Calls in flight per provider. Each vendor has its own limits, so different vendors can run
# side by side.
_CONCURRENCY = int(os.environ.get("NYCYNAPSE_LLM_CONCURRENCY", "2"))
_locks: dict[str, threading.BoundedSemaphore] = {}
_locks_guard = threading.Lock()


def _lock_for(pid: str) -> threading.BoundedSemaphore:
    with _locks_guard:
        if pid not in _locks:
            _locks[pid] = threading.BoundedSemaphore(_CONCURRENCY)
        return _locks[pid]


_config: dict | None = None


class LLMError(Exception):
    pass


class ProviderLimit(Exception):  # noqa: N818
    """The provider refused for quota reasons. Not an answer failure: callers should stop and
    resume later rather than record it against the question."""


LIMIT_MARKERS = ("session limit", "usage limit", "rate limit", "quota", "too many requests")


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None

    def json(self) -> Any:
        return parse_json(self.text)


def config() -> dict:
    global _config
    if _config is None:
        path = os.environ.get("NYCYNAPSE_PROVIDERS", "/root/.secrets/nycynapse-providers.json")
        if not os.path.isfile(path):
            raise LLMError(f"no provider config at {path}")
        with open(path) as fh:
            _config = json.load(fh)
    return _config


# Roles that stay put when a run switches the model under test: grading and auditing must not
# change with the model being graded.
FIXED_ROLES = ("judge", "auditor")


def use_model(pid: str) -> None:
    """Point every pipeline role at one provider, for an evaluation run of that model."""
    cfg = config()
    if pid not in cfg["providers"]:
        raise LLMError(f"unknown provider {pid}; known: {', '.join(cfg['providers'])}")
    fixed = {r: cfg["roles"][r] for r in FIXED_ROLES if r in cfg["roles"]}
    cfg["roles"] = {"default": pid, **fixed}


def provider_for(role: str) -> tuple[str, dict]:
    cfg = config()
    pid = cfg["roles"].get(role, cfg["roles"]["default"])
    return pid, cfg["providers"][pid]


def parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise LLMError(f"no JSON in reply: {text[:200]}")
    return json.loads(text[start:])


def _anthropic(pid: str, p: dict, system: str, prompt: str, schema: dict | None) -> LLMResult:
    key = os.environ.get(p.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    t0 = time.monotonic()
    r = httpx.post(
        p.get("base_url", "https://api.anthropic.com") + "/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": p["model"],
            "max_tokens": p.get("max_tokens", 4000),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=p.get("timeout", 180),
    )
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
    d = r.json()
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    u = d.get("usage", {})
    return LLMResult(
        text,
        p["model"],
        pid,
        int((time.monotonic() - t0) * 1000),
        u.get("input_tokens"),
        u.get("output_tokens"),
    )


def _openai(pid: str, p: dict, system: str, prompt: str, schema: dict | None) -> LLMResult:
    key = os.environ.get(p.get("api_key_env", "OPENAI_API_KEY"), "")
    body: dict[str, Any] = {
        "model": p["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    if schema and p.get("json_schema", True):
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        }
    t0 = time.monotonic()
    r = httpx.post(
        p.get("base_url", "https://api.openai.com/v1") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
        timeout=p.get("timeout", 180),
    )
    if r.status_code >= 400:
        raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
    d = r.json()
    u = d.get("usage", {})
    return LLMResult(
        d["choices"][0]["message"]["content"],
        p["model"],
        pid,
        int((time.monotonic() - t0) * 1000),
        u.get("prompt_tokens"),
        u.get("completion_tokens"),
    )


def _command(pid: str, p: dict, system: str, prompt: str, schema: dict | None) -> LLMResult:
    if p.get("system_in_prompt"):
        # For tools with no system prompt option: mark the instructions off clearly so they
        # read as the rules for the task, not as part of the question. These tools are coding
        # agents; left alone they try to run commands to explore, which ends the turn empty.
        system = (
            f"{system}\n\nAnswer from this message alone. Do not use tools, run commands or "
            "read files: everything you need is below. Reply with the answer only."
        )
        prompt = f"<instructions>\n{system}\n</instructions>\n\n<task>\n{prompt}\n</task>"
    with tempfile.TemporaryDirectory() as td:
        schema_path, out_path = os.path.join(td, "schema.json"), os.path.join(td, "out.txt")
        if schema is not None:
            with open(schema_path, "w") as fh:
                json.dump(schema, fh)
        # Some tools only take the prompt as an argument; Linux caps one argument at 128 KB.
        if p.get("prompt_arg") and len(prompt.encode()) > 120_000:
            raise LLMError("prompt too long to pass as an argument")
        subst = {
            "{prompt}": prompt if p.get("prompt_arg") else "",
            "{system}": system,
            "{schema_file}": schema_path,
            "{out_file}": out_path,
            "{model}": p.get("model", ""),
            "{effort}": p.get("effort", "low"),
            "{schema_json}": json.dumps(schema) if schema is not None else "",
        }
        argv: list[str] = []
        for a in p["argv"]:
            if a in ("{schema_file}", "{schema_json}") and schema is None:
                if argv:
                    argv.pop()
                continue
            for k, v in subst.items():
                a = a.replace(k, v)
            argv.append(a)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                input=None if p.get("prompt_arg") else prompt,
                capture_output=True,
                text=True,
                timeout=p.get("timeout", 300),
                cwd=p.get("cwd", td),
                env=dict(os.environ, NO_COLOR="1"),
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError("model call timed out") from e
        ms = int((time.monotonic() - t0) * 1000)
        out = proc.stdout
        if os.path.exists(out_path):
            with open(out_path) as fh:
                out = fh.read() or out
    if p.get("parse") == "json_result":
        try:
            d = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"unreadable reply: {(proc.stderr or proc.stdout)[:300]}") from e
        if d.get("is_error"):
            raise LLMError(f"{d.get('subtype')}: {str(d.get('result'))[:300]}")
        text = d.get("result", "")
        if isinstance(d.get("structured_output"), dict | list):
            text = json.dumps(d["structured_output"])
        u = d.get("usage") or {}
        tin = (
            sum(
                u.get(k) or 0
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
            or None
        )
        return LLMResult(
            text, p.get("model", ""), pid, ms, tin, u.get("output_tokens"), d.get("total_cost_usd")
        )
    if p.get("parse") == "agy_json":
        return _agy_result(pid, p, proc, ms)
    if p.get("parse") == "codex_jsonl":
        return _codex_result(pid, p, proc, out, ms)
    if proc.returncode != 0 and not out.strip():
        raise LLMError(f"exited {proc.returncode}: {proc.stderr[:300]}")
    return LLMResult(out.strip(), p.get("model", ""), pid, ms)


def _agy_result(pid: str, p: dict, proc, ms: int) -> LLMResult:
    """Antigravity print mode: a JSON envelope with status, structured_output and usage."""
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"unreadable reply: {(proc.stderr or proc.stdout)[:300]}") from e
    if d.get("status") != "SUCCESS":
        raise LLMError(f"{d.get('status')}: {str(d.get('error') or d.get('response'))[:300]}")
    so = d.get("structured_output")
    text = json.dumps(so) if isinstance(so, dict | list) else d.get("response", "")
    if not text.strip():
        # A turn that ended in a refused tool call reports success with nothing in it.
        raise LLMError(f"empty reply: {proc.stderr.strip()[-200:]}")
    u = d.get("usage") or {}
    tin = u.get("input_tokens")
    tout = (u.get("output_tokens") or 0) + (u.get("thinking_tokens") or 0)
    return LLMResult(text, p.get("model", ""), pid, ms, tin, tout, _list_price(p, tin, tout))


def _codex_result(pid: str, p: dict, proc, out: str, ms: int) -> LLMResult:
    """codex exec --json: the reply is in the -o file, usage in the turn.completed event."""
    usage, error = {}, None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
        elif event.get("type") in ("turn.failed", "error"):
            error = json.dumps(event)[:300]
    if not out.strip() or out.strip() == proc.stdout.strip():
        raise LLMError(error or f"no reply: {proc.stderr[-300:]}")
    tin = usage.get("input_tokens")
    tout = (usage.get("output_tokens") or 0) + (usage.get("reasoning_output_tokens") or 0)
    return LLMResult(out.strip(), p.get("model", ""), pid, ms, tin, tout, _list_price(p, tin, tout))


def _list_price(p: dict, tokens_in: int | None, tokens_out: int | None) -> float | None:
    """Cost at the provider's published per-token price, for subscriptions that do not bill
    per call. Prices are per million tokens."""
    price = p.get("price_per_mtok")
    if not price or tokens_in is None:
        return None
    return (tokens_in * price["input"] + (tokens_out or 0) * price["output"]) / 1_000_000


_IMPL = {"anthropic": _anthropic, "openai": _openai, "command": _command}


def complete(
    role: str, system: str, prompt: str, schema: dict | None = None, retries: int = 2
) -> LLMResult:
    pid, _ = provider_for(role)
    return complete_with(pid, system, prompt, schema, retries)


def complete_with(
    pid: str, system: str, prompt: str, schema: dict | None = None, retries: int = 2
) -> LLMResult:
    """Call one named provider, with retries, stopping cleanly on quota errors."""
    p = config()["providers"][pid]
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with _lock_for(pid):
                return _IMPL[p["type"]](pid, p, system, prompt, schema)
        except LLMError as e:
            if any(m in str(e).lower() for m in LIMIT_MARKERS):
                raise ProviderLimit(str(e)[:300]) from e
            last = e
            time.sleep(3 * (attempt + 1))
    raise LLMError(f"{pid} failed after {retries + 1} attempts: {last}")
