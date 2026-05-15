"""Resource governor — caps CPU / RAM / disk-I/O usage to keep the host machine responsive.

Workers call ``acquire()`` before each unit of work. The governor blocks until
system resource usage is below the configured ceilings. A background sampler
thread updates the rolling measurements every ``sample_interval`` seconds.

Three named presets cover the common cases:
    - background : 50% CPU, idle priority, low I/O priority
    - balanced   : 90% CPU, below-normal priority (default)
    - fullspeed  : no caps, normal priority
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThrottleConfig:
    name: str
    cpu_cap: float          # 0..100, the % above which we hold off
    ram_cap: float          # 0..100
    io_cap_mbps: float | None  # None = no cap
    process_priority: str   # 'idle' | 'below_normal' | 'normal'

    @classmethod
    def background(cls) -> ThrottleConfig:
        return cls("background", cpu_cap=50.0, ram_cap=80.0, io_cap_mbps=None, process_priority="idle")

    @classmethod
    def balanced(cls) -> ThrottleConfig:
        return cls("balanced", cpu_cap=90.0, ram_cap=85.0, io_cap_mbps=None, process_priority="below_normal")

    @classmethod
    def fullspeed(cls) -> ThrottleConfig:
        return cls("fullspeed", cpu_cap=100.0, ram_cap=95.0, io_cap_mbps=None, process_priority="normal")

    @classmethod
    def custom(cls, cpu_cap: float, ram_cap: float, io_cap_mbps: float | None) -> ThrottleConfig:
        return cls("custom", cpu_cap=cpu_cap, ram_cap=ram_cap, io_cap_mbps=io_cap_mbps, process_priority="below_normal")


PRESETS = {
    "background": ThrottleConfig.background(),
    "balanced": ThrottleConfig.balanced(),
    "fullspeed": ThrottleConfig.fullspeed(),
}


def _apply_process_priority(priority: str) -> None:
    """Set the current process priority. Platform-specific; best-effort."""
    try:
        proc = psutil.Process(os.getpid())
        if sys.platform == "win32":
            mapping = {
                "idle": psutil.IDLE_PRIORITY_CLASS,
                "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                "normal": psutil.NORMAL_PRIORITY_CLASS,
            }
            proc.nice(mapping.get(priority, psutil.BELOW_NORMAL_PRIORITY_CLASS))
        else:
            mapping = {"idle": 19, "below_normal": 10, "normal": 0}
            proc.nice(mapping.get(priority, 10))
    except (psutil.AccessDenied, psutil.NoSuchProcess, ValueError) as e:
        log.warning("could not set process priority to %s: %s", priority, e)


class Governor:
    """Background sampler + token gate.

    Usage::

        gov = Governor(ThrottleConfig.balanced())
        gov.start()
        ...
        gov.acquire()        # workers call this before each file
        ...
        gov.stop()
    """

    def __init__(self, config: ThrottleConfig, sample_interval: float = 0.5):
        self._cfg = config
        self._interval = sample_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu_pct: float = 0.0
        self._ram_pct: float = 0.0
        self._io_mbps: float = 0.0
        self._last_io_bytes: int | None = None
        self._last_io_ts: float | None = None

    @property
    def config(self) -> ThrottleConfig:
        return self._cfg

    def set_config(self, config: ThrottleConfig) -> None:
        self._cfg = config
        _apply_process_priority(config.process_priority)

    def start(self) -> None:
        _apply_process_priority(self._cfg.process_priority)
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="governor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def snapshot(self) -> dict[str, float]:
        return {"cpu_pct": self._cpu_pct, "ram_pct": self._ram_pct, "io_mbps": self._io_mbps}

    def acquire(self) -> None:
        """Block until current usage is below the configured caps.

        Spins in 50 ms sleeps. Cheap; the sampler thread is what actually does the work.
        """
        while not self._has_capacity():
            time.sleep(0.05)

    def _has_capacity(self) -> bool:
        cfg = self._cfg
        return not (
            self._cpu_pct > cfg.cpu_cap
            or self._ram_pct > cfg.ram_cap
            or (cfg.io_cap_mbps is not None and self._io_mbps > cfg.io_cap_mbps)
        )

    def _run(self) -> None:
        # Prime psutil.cpu_percent so the first reading isn't 0.0
        psutil.cpu_percent(interval=None)
        while not self._stop.is_set():
            try:
                self._cpu_pct = psutil.cpu_percent(interval=None)
                self._ram_pct = psutil.virtual_memory().percent
                self._sample_io()
            except Exception as e:  # noqa: BLE001
                log.debug("governor sample failed: %s", e)
            self._stop.wait(self._interval)

    def _sample_io(self) -> None:
        io = psutil.disk_io_counters()
        if io is None:
            return
        bytes_now = io.read_bytes
        ts_now = time.monotonic()
        if self._last_io_bytes is not None and self._last_io_ts is not None:
            dt = max(ts_now - self._last_io_ts, 1e-6)
            self._io_mbps = (bytes_now - self._last_io_bytes) / dt / 1_048_576.0
        self._last_io_bytes = bytes_now
        self._last_io_ts = ts_now
