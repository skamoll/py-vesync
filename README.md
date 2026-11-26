# py-vesync

Lightweight sync/async Python client for the VeSync Cloud API v2.

## Installation

```bash
pip install -e ".[dev]"
```

## CLI Usage

```bash
export VESYNC_EMAIL="you@example.com"
export VESYNC_PASSWORD="your-password"
vesync-cli devices
vesync-cli status
vesync-cli presets
vesync-cli history --all
vesync-cli monitor --output sessions.jsonl
```

## Library Usage

```python
from py_vesync import AsyncVeSyncClient, CookSessionTracker

async with AsyncVeSyncClient(region="EU") as client:
    await client.login("you@example.com", "your-password")
    devices = await client.get_devices_full()
```
