"""Lightweight VeSync Cloud API v2 client (sync + async)."""

from .client import (
    AsyncVeSyncClient,
    VeSyncAPIError,
    VeSyncAuthError,
    VeSyncClient,
    VeSyncDevice,
    is_airfryer_running,
    normalize_program_key,
)
from .monitor import AirFryerMonitor, CookSession, CookSessionTracker

__all__ = [
    "AirFryerMonitor",
    "AsyncVeSyncClient",
    "CookSession",
    "CookSessionTracker",
    "VeSyncAPIError",
    "VeSyncAuthError",
    "VeSyncClient",
    "VeSyncDevice",
    "is_airfryer_running",
    "normalize_program_key",
]
