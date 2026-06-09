from __future__ import annotations

import ctypes
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from docvec.crawler import CrawlSummary

logger = logging.getLogger(__name__)

DEFAULT_AUTO_SCAN_ROOTS = (Path(r"C:\\"), Path(r"D:\\"), Path(r"E:\\"))


@dataclass(frozen=True)
class AutoScanConfig:
    enabled: bool = True
    interval_seconds: int = 3600
    idle_seconds: int = 600
    require_charging: bool = True
    roots: tuple[Path, ...] = DEFAULT_AUTO_SCAN_ROOTS
    check_interval_seconds: int = 60


@dataclass(frozen=True)
class PowerIdleState:
    is_charging: bool
    idle_seconds: float
    source: str


class AutoScanScheduler:
    def __init__(
        self,
        *,
        start_scan: Callable[[list[Path]], CrawlSummary],
        is_scan_running: Callable[[], bool],
        read_power_idle: Callable[[], PowerIdleState] | None = None,
        now: Callable[[], datetime] | None = None,
        config: AutoScanConfig | None = None,
    ) -> None:
        self._start_scan = start_scan
        self._is_scan_running = is_scan_running
        self._read_power_idle = read_power_idle or read_windows_power_idle_state
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._config = config or AutoScanConfig()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_triggered_at: datetime | None = None
        self._last_checked_at: datetime | None = None
        self._last_skip_reason = ""
        self._last_environment: PowerIdleState | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="docvec-auto-scan",
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def configure(self, config: AutoScanConfig) -> None:
        with self._lock:
            self._config = config
            self._last_skip_reason = "configured"

    def config(self) -> AutoScanConfig:
        with self._lock:
            return self._config

    def tick(self) -> bool:
        with self._lock:
            config = self._config
            now = self._now()
            self._last_checked_at = now

            if not config.enabled:
                self._set_skip_reason("disabled")
                return False

            if self._is_scan_running():
                self._set_skip_reason("scan_running")
                return False

            if (
                self._last_triggered_at is not None
                and (now - self._last_triggered_at).total_seconds()
                < config.interval_seconds
            ):
                self._set_skip_reason("waiting_for_interval")
                return False

            environment = self._read_power_idle()
            self._last_environment = environment
            if config.require_charging and not environment.is_charging:
                self._set_skip_reason("waiting_for_charging")
                return False
            if environment.idle_seconds < config.idle_seconds:
                self._set_skip_reason("waiting_for_idle")
                return False

            try:
                self._start_scan(list(config.roots))
            except Exception:
                logger.exception("Auto-scan trigger failed")
                self._set_skip_reason("start_failed")
                return False
            self._last_triggered_at = now
            self._last_skip_reason = ""
            logger.info("Auto-scan triggered roots=%s", [str(root) for root in config.roots])
            return True

    def status(self) -> dict:
        with self._lock:
            config = self._config
            next_due_at = None
            if self._last_triggered_at is not None:
                next_due_at = (
                    self._last_triggered_at.timestamp() + config.interval_seconds
                )
            return {
                "enabled": config.enabled,
                "interval_seconds": config.interval_seconds,
                "idle_seconds": config.idle_seconds,
                "require_charging": config.require_charging,
                "roots": [str(root) for root in config.roots],
                "check_interval_seconds": config.check_interval_seconds,
                "last_checked_at": _iso(self._last_checked_at),
                "last_triggered_at": _iso(self._last_triggered_at),
                "next_due_at": (
                    datetime.fromtimestamp(next_due_at, tz=timezone.utc).isoformat()
                    if next_due_at is not None
                    else ""
                ),
                "last_skip_reason": self._last_skip_reason,
                "power_idle": (
                    {
                        "is_charging": self._last_environment.is_charging,
                        "idle_seconds": self._last_environment.idle_seconds,
                        "source": self._last_environment.source,
                    }
                    if self._last_environment is not None
                    else None
                ),
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Auto-scan tick failed")
                with self._lock:
                    self._last_skip_reason = "tick_failed"
            wait_seconds = max(1, self.config().check_interval_seconds)
            self._stop_event.wait(wait_seconds)

    def _set_skip_reason(self, reason: str) -> None:
        self._last_skip_reason = reason
        logger.info("Auto-scan skipped reason=%s", reason)


def read_windows_power_idle_state() -> PowerIdleState:
    return PowerIdleState(
        is_charging=_is_ac_power_online(),
        idle_seconds=_idle_seconds(),
        source="windows",
    )


def _is_ac_power_online() -> bool:
    class SystemPowerStatus(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("Reserved1", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    status = SystemPowerStatus()
    try:
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))  # type: ignore[attr-defined]
    except AttributeError:
        return False
    if not ok:
        return False
    return int(status.ACLineStatus) == 1


def _idle_seconds() -> float:
    class LastInputInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_uint),
        ]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(LastInputInfo)
    try:
        ok = ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))  # type: ignore[attr-defined]
        tick_count = ctypes.windll.kernel32.GetTickCount()
    except AttributeError:
        return 0.0
    if not ok:
        return 0.0
    elapsed_ms = (int(tick_count) - int(info.dwTime)) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""
