"""
history.py — Time-based metric buffers.

Stores timestamped (epoch, value) samples and supports configurable
retention windows.  The default keeps 5 minutes of data; the frontend
receives the full buffer and can slice as needed.
"""

import time
from collections import deque
from typing import List, Tuple

DEFAULT_WINDOW_SECONDS = 300  # 5 minutes


class MetricHistory:
    """Rolling time-based buffer for CPU and memory usage history."""

    def __init__(self, window: int = DEFAULT_WINDOW_SECONDS) -> None:
        self._window = window
        self._cpu: deque[Tuple[float, float]] = deque()
        self._memory: deque[Tuple[float, float]] = deque()

    def add(self, cpu_pct: float, mem_pct: float) -> None:
        """Append a new timestamped sample and prune old entries."""
        now = time.time()
        self._cpu.append((now, round(cpu_pct, 1)))
        self._memory.append((now, round(mem_pct, 1)))
        self._prune(now)

    def _prune(self, now: float) -> None:
        """Remove entries older than the retention window."""
        cutoff = now - self._window
        while self._cpu and self._cpu[0][0] < cutoff:
            self._cpu.popleft()
        while self._memory and self._memory[0][0] < cutoff:
            self._memory.popleft()

    def get_cpu_history(self) -> List[float]:
        """Return CPU values (without timestamps) for chart rendering."""
        return [v for _, v in self._cpu]

    def get_memory_history(self) -> List[float]:
        """Return memory values (without timestamps) for chart rendering."""
        return [v for _, v in self._memory]

    @property
    def window(self) -> int:
        return self._window

    @window.setter
    def window(self, seconds: int) -> None:
        self._window = max(60, seconds)  # minimum 1 minute
        self._prune(time.time())
