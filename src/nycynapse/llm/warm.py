"""Model processes started ahead of need.

A model reached through a command-line tool spends about two and a half seconds starting up
on every call, before it reads a word of the prompt. A turn makes four or five calls, most of
them with the same instructions (the agent's step and the fact check). So a few processes are
started early, in the tool's streaming mode, each waiting with its instructions and output
schema already loaded; a call takes one, sends its prompt as a single message, reads the
result and ends the process. A fresh one is started for the next call while this one works.

Only instructions used at least twice in the last quarter hour are kept warm, a small total
cap keeps memory in check (each waiting process holds about 200 MB), and processes that wait
longer than ten minutes are ended. When none is ready the call starts its own, as before.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ENABLED = os.environ.get("NYCYNAPSE_WARM_MODELS", "") == "1"
MAX_IDLE = int(os.environ.get("NYCYNAPSE_WARM_MAX", "3"))
MAX_AGE_S = 600
HOT_WINDOW_S = 900
STREAM = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]


def usable(p: dict) -> bool:
    """Providers whose tool can stream: the ones read as one JSON result on stdout."""
    return ENABLED and p.get("parse") == "json_result" and not p.get("prompt_arg")


def stream_argv(argv: list[str]) -> list[str]:
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--output-format" and i + 1 < len(argv):
            skip = True
            continue
        out.append(a)
    return out + STREAM


@dataclass
class _Proc:
    popen: subprocess.Popen
    lines: queue.Queue
    folder: str
    born: float = field(default_factory=time.monotonic)

    def alive(self) -> bool:
        return self.popen.poll() is None

    def end(self) -> None:
        try:
            self.popen.kill()
            self.popen.wait(timeout=5)
        except Exception:  # noqa: BLE001 - already gone
            pass
        shutil.rmtree(self.folder, ignore_errors=True)


def _spawn(argv: list[str]) -> _Proc:
    folder = tempfile.mkdtemp(prefix="warm-")
    popen = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=folder,
        env=dict(os.environ, NO_COLOR="1"),
    )
    lines: queue.Queue = queue.Queue()

    def read() -> None:
        for raw in popen.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=read, daemon=True, name="warm-read").start()
    return _Proc(popen, lines, folder)


class Pool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple, list[_Proc]] = {}
        self._uses: dict[tuple, list[float]] = {}
        self._starting: dict[tuple, int] = {}
        self._janitor = False

    def _hot(self, key: tuple) -> bool:
        now = time.monotonic()
        uses = [t for t in self._uses.get(key, []) if now - t < HOT_WINDOW_S]
        self._uses[key] = uses
        return len(uses) >= 2

    def take(self, key: tuple, argv: list[str]) -> tuple[_Proc, bool]:
        """A process for this call, and whether it was already waiting."""
        with self._lock:
            self._uses.setdefault(key, []).append(time.monotonic())
            ready = None
            waiting = self._idle.get(key, [])
            while waiting:
                p = waiting.pop(0)
                if p.alive() and time.monotonic() - p.born < MAX_AGE_S:
                    ready = p
                    break
                threading.Thread(target=p.end, daemon=True).start()
            refill = self._hot(key) and not self._idle.get(key) and not self._starting.get(key)
            if refill:
                self._starting[key] = 1
            if not self._janitor:
                self._janitor = True
                threading.Thread(target=self._sweep, daemon=True, name="warm-sweep").start()
        if refill:
            threading.Thread(target=self._refill, args=(key, argv), daemon=True).start()
        return (ready, True) if ready else (_spawn(argv), False)

    def _refill(self, key: tuple, argv: list[str]) -> None:
        try:
            p = _spawn(argv)
        except Exception as e:  # noqa: BLE001 - the next call starts its own
            log.warning("could not start a waiting model process: %s", e)
            with self._lock:
                self._starting.pop(key, None)
            return
        extra: list[_Proc] = []
        with self._lock:
            self._starting.pop(key, None)
            self._idle.setdefault(key, []).append(p)
            # Over the cap: end the oldest waiting processes of other instructions first.
            allp = sorted(
                ((q.born, k, q) for k, qs in self._idle.items() for q in qs), key=lambda x: x[0]
            )
            while len(allp) > MAX_IDLE:
                _, k, q = allp.pop(0)
                self._idle[k].remove(q)
                extra.append(q)
        for q in extra:
            q.end()

    def _sweep(self) -> None:
        while True:
            time.sleep(30)
            old: list[_Proc] = []
            with self._lock:
                for k, qs in self._idle.items():
                    keep = []
                    for q in qs:
                        stale = time.monotonic() - q.born >= MAX_AGE_S or not q.alive()
                        (old if stale else keep).append(q)
                    self._idle[k] = keep
            for q in old:
                q.end()

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._idle.values())


pool = Pool()


def call(key: tuple, argv: list[str], prompt: str, timeout: float) -> tuple[dict, int, bool]:
    """Send one prompt to a waiting (or new) process; returns the tool's result event, the
    milliseconds from sending to the result, and whether the process was waiting already."""
    proc, warm = pool.take(key, stream_argv(argv))
    t0 = time.monotonic()
    try:
        msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        proc.popen.stdin.write((json.dumps(msg) + "\n").encode())
        proc.popen.stdin.flush()
        deadline = t0 + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            raw = proc.lines.get(timeout=left)
            if raw is None:
                raise EOFError("the model process ended without a result")
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") == "result":
                return event, int((time.monotonic() - t0) * 1000), warm
    except queue.Empty as e:
        raise TimeoutError from e
    finally:
        threading.Thread(target=proc.end, daemon=True).start()
