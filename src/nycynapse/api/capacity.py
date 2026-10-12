"""How many conversations are answered at once, and the line for the rest.

A few answers run at the same time (each model call is a separate process, and the machine is
small). A short line forms behind them; everyone in it is told their place, and it moves first
come, first served. When the line is full, a new question is turned away at once with a clear
message instead of waiting without end.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass


@dataclass
class Ticket:
    n: int
    cancelled: bool = False


class Full(Exception):  # noqa: N818
    pass


class Gate:
    def __init__(self, active: int, waiting: int):
        self.active_max = active
        self.waiting_max = waiting
        self._cond = threading.Condition()
        self._active = 0
        self._line: list[Ticket] = []
        self._numbers = itertools.count(1)

    def join(self) -> Ticket:
        with self._cond:
            if self._active < self.active_max and not self._line:
                self._active += 1
                return Ticket(0)
            if len(self._line) >= self.waiting_max:
                raise Full
            t = Ticket(next(self._numbers))
            self._line.append(t)
            return t

    def position(self, t: Ticket) -> int:
        """0 once the ticket may run, else its place in line (1 is next)."""
        with self._cond:
            return self._line.index(t) + 1 if t in self._line else 0

    def wait(self, t: Ticket, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a turn; True when it is this ticket's turn."""
        with self._cond:
            if t.n == 0 or t not in self._line:
                return True
            self._cond.wait_for(
                lambda: self._line and self._line[0] is t and self._active < self.active_max,
                timeout=timeout,
            )
            if self._line and self._line[0] is t and self._active < self.active_max:
                self._line.pop(0)
                self._active += 1
                t.n = 0
                self._cond.notify_all()
                return True
            return False

    def leave_line(self, t: Ticket) -> None:
        with self._cond:
            if t in self._line:
                self._line.remove(t)
                self._cond.notify_all()

    def done(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            return {
                "active": self._active,
                "waiting": len(self._line),
                "active_max": self.active_max,
                "waiting_max": self.waiting_max,
            }
