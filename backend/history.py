"""
history.py — Time-based metric buffers.

Stores timestamped (epoch, value) samples and supports configurable
retention windows.  The default keeps 5 minutes of data; the frontend
receives the full buffer and can slice as needed.
"""

import time
from collections import deque
from typing import List, Tuple

DEFAULT_WINDOW_SECONDS = 300


class MetricHistory:
    def __init__(self, window: int = DEFAULT_WINDOW_SECONDS) -> None:
        self._window = window
        self._cpu:        deque[Tuple[float, float]] = deque()
        self._memory:     deque[Tuple[float, float]] = deque()
        self._disk_read:  deque[Tuple[float, float]] = deque()
        self._disk_write: deque[Tuple[float, float]] = deque()
        self._net_up:     deque[Tuple[float, float]] = deque()
        self._net_down:   deque[Tuple[float, float]] = deque()
        self._load_one:   deque[Tuple[float, float]] = deque()
        self._load_five:  deque[Tuple[float, float]] = deque()
        self._load_fifteen: deque[Tuple[float, float]] = deque()

    def add(self, cpu_pct: float, mem_pct: float,
            disk_read: float = 0.0, disk_write: float = 0.0,
            net_up: float = 0.0, net_down: float = 0.0,
            load_one: float = 0.0, load_five: float = 0.0,
            load_fifteen: float = 0.0) -> None:
        now = time.time()
        self._cpu.append((now,        round(cpu_pct, 1)))
        self._memory.append((now,     round(mem_pct, 1)))
        self._disk_read.append((now,  round(disk_read, 2)))
        self._disk_write.append((now, round(disk_write, 2)))
        self._net_up.append((now,     round(net_up, 2)))
        self._net_down.append((now,   round(net_down, 2)))
        self._load_one.append((now,   round(load_one, 2)))
        self._load_five.append((now,  round(load_five, 2)))
        self._load_fifteen.append((now, round(load_fifteen, 2)))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for dq in (self._cpu, self._memory, self._disk_read,
                   self._disk_write, self._net_up, self._net_down,
                   self._load_one, self._load_five, self._load_fifteen):
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def _vals(self, dq) -> List[float]:
        return [v for _, v in dq]

    def get_cpu_history(self):     return self._vals(self._cpu)
    def get_memory_history(self):  return self._vals(self._memory)
    def get_disk_history(self):
        return {"read": self._vals(self._disk_read), "write": self._vals(self._disk_write)}
    def get_network_history(self):
        return {"up": self._vals(self._net_up), "down": self._vals(self._net_down)}
    def get_load_history(self):
        return {
            "one": self._vals(self._load_one),
            "five": self._vals(self._load_five),
            "fifteen": self._vals(self._load_fifteen),
        }

    @property
    def window(self) -> int:
        return self._window
