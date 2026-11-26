"""Adaptive session tracking and polling monitor for VeSync air fryers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import VeSyncAPIError, VeSyncClient, is_airfryer_running, normalize_program_key


@dataclass(slots=True)
class CookSession:
    """Measured cooking session between leaving standby and returning to standby."""

    cid: str
    device_name: str
    started_at: str
    ended_at: str | None = None
    actual_seconds: float | None = None
    cook_mode: str = ""
    cook_mode_key: str | None = None
    program_name: str | None = None
    program_key: str | None = None
    programmed_seconds: int | None = None
    programmed_temp: int | None = None
    temp_unit: str = "c"
    initial_temp: int | None = None
    peak_temp: int | None = None
    last_temp: int | None = None
    statuses_seen: list[str] = field(default_factory=list)
    _start_mono: float = field(default=0.0, repr=False)


class CookSessionTracker:
    """Pure state machine for Home Assistant DataUpdateCoordinator and AirFryerMonitor."""

    def __init__(
        self,
        cid: str,
        *,
        device_name: str = "AirFryer",
        standby_interval: float = 10.0,
        active_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc).astimezone(),
    ) -> None:
        self.cid = cid
        self.device_name = device_name
        self.standby_interval = standby_interval
        self.active_interval = active_interval
        self._clock = clock
        self._wall_clock = wall_clock
        self.active_session: CookSession | None = None
        self.last_completed_session: CookSession | None = None

    def enrich_from_history_entry(self, entry: dict[str, Any] | None) -> None:
        """Attach program name, normalized key, and programmed duration/temp from a getCookingHistoryV2 item."""
        if self.active_session is None or not isinstance(entry, dict):
            return
        self.active_session.program_name = entry.get("recipeName")
        self.active_session.program_key = normalize_program_key(self.active_session.program_name)
        wp = entry.get("workParam") or {}
        if isinstance(wp, dict):
            self.active_session.programmed_seconds = wp.get("workTime")
            self.active_session.programmed_temp = wp.get("workTemp")

    @property
    def current_elapsed_seconds(self) -> float:
        """Live elapsed seconds for the ongoing cooking session, or 0.0 when idle."""
        if self.active_session is None:
            return 0.0
        return round(max(0.0, self._clock() - self.active_session._start_mono), 1)

    def update(self, status: dict[str, Any]) -> tuple[bool, float, CookSession | None]:
        """Process one status snapshot; returns (is_running, recommended_interval_s, completed_session)."""
        running = is_airfryer_running(status)
        cook_status = str(status.get("cookStatus") or "")
        temp = status.get("currentTemp")
        now_mono = self._clock()
        now_iso = self._wall_clock().isoformat(timespec="seconds")

        if running:
            if self.active_session is None:
                self.active_session = CookSession(
                    cid=self.cid,
                    device_name=self.device_name,
                    started_at=now_iso,
                    cook_mode=str(status.get("cookMode") or ""),
                    cook_mode_key=normalize_program_key(status.get("cookMode")),
                    temp_unit=str(status.get("tempUnit") or "c"),
                    initial_temp=temp,
                    peak_temp=temp,
                    last_temp=temp,
                    statuses_seen=[cook_status] if cook_status else [],
                    _start_mono=now_mono,
                )
            else:
                sess = self.active_session
                sess.last_temp = temp
                if temp is not None and (sess.peak_temp is None or temp > sess.peak_temp):
                    sess.peak_temp = temp
                if cook_status and (not sess.statuses_seen or sess.statuses_seen[-1] != cook_status):
                    sess.statuses_seen.append(cook_status)
            return True, self.active_interval, None

        completed: CookSession | None = None
        if self.active_session is not None:
            sess = self.active_session
            sess.ended_at = now_iso
            sess.actual_seconds = round(max(0.0, now_mono - sess._start_mono), 1)
            sess.last_temp = temp
            self.active_session = None
            self.last_completed_session = sess
            completed = sess

        return False, self.standby_interval, completed


class AirFryerMonitor:
    """Polls an air fryer at standby_interval (10s) and speeds up to active_interval (5s) when running."""

    def __init__(
        self,
        client: VeSyncClient,
        cid: str,
        config_module: str,
        *,
        device_name: str = "AirFryer",
        standby_interval: float = 10.0,
        active_interval: float = 5.0,
        relogin: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc).astimezone(),
    ) -> None:
        self.client = client
        self.cid = cid
        self.config_module = config_module
        self._relogin = relogin
        self.tracker = CookSessionTracker(
            cid,
            device_name=device_name,
            standby_interval=standby_interval,
            active_interval=active_interval,
            clock=clock,
            wall_clock=wall_clock,
        )

    @property
    def active_session(self) -> CookSession | None:
        return self.tracker.active_session

    def _enrich_from_history(self) -> None:
        try:
            page = self.client.get_cooking_history(self.cid, self.config_module, page_size=1)
            latest = (page.get("cookHistoryList") or [None])[0]
            self.tracker.enrich_from_history_entry(latest)
        except Exception:
            pass

    def poll_once(self) -> tuple[dict[str, Any], float, CookSession | None]:
        """Poll status once; returns (status, next_interval_s, completed_session_or_None)."""
        try:
            status = self.client.get_airfryer_status(self.cid, self.config_module)
        except VeSyncAPIError:
            if self._relogin is None:
                raise
            self._relogin()
            status = self.client.get_airfryer_status(self.cid, self.config_module)

        running, interval, completed = self.tracker.update(status)
        if running and self.tracker.active_session is not None and not self.tracker.active_session.program_name:
            self._enrich_from_history()
        return status, interval, completed

    def run(
        self,
        *,
        on_tick: Callable[[dict[str, Any], float], None] | None = None,
        on_complete: Callable[[CookSession], None] | None = None,
        max_cycles: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Continuously poll until max_cycles is reached or KeyboardInterrupt."""
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            status, interval, completed = self.poll_once()
            if on_tick is not None:
                on_tick(status, interval)
            if completed is not None and on_complete is not None:
                on_complete(completed)
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleeper(interval)
