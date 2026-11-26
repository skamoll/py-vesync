"""CLI for py-vesync."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from .client import VeSyncClient, is_airfryer_running
from .monitor import AirFryerMonitor, CookSession


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vesync-cli", description="Lightweight VeSync Cloud CLI")
    parser.add_argument("--region", default=os.environ.get("VESYNC_REGION", "EU"), choices=("EU", "US"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="List devices")
    st = sub.add_parser("status", help="Get air fryer status for all or one device")
    st.add_argument("--cid", help="Optional device cid")
    pr = sub.add_parser("presets", help="Get air fryer preset programs")
    pr.add_argument("--cid", help="Optional device cid")
    hs = sub.add_parser("history", help="Get cooking history")
    hs.add_argument("--cid", help="Optional device cid")
    hs.add_argument("--limit", type=int, default=10, help="Page size (default: 10)")
    hs.add_argument("--all", action="store_true", help="Fetch all pages")
    mon = sub.add_parser("monitor", help="Adaptive polling monitor (10s standby, 5s active)")
    mon.add_argument("--cid", help="Optional device cid")
    mon.add_argument("--standby-interval", type=float, default=10.0)
    mon.add_argument("--active-interval", type=float, default=5.0)
    mon.add_argument("--output", help="Optional JSONL path to append completed sessions")

    args = parser.parse_args(argv)
    email = os.environ.get("VESYNC_EMAIL", "")
    password = os.environ.get("VESYNC_PASSWORD", "")
    if not email or not password:
        print("Set VESYNC_EMAIL and VESYNC_PASSWORD environment variables", file=sys.stderr)
        return 2

    with VeSyncClient(region=args.region) as client:
        client.login(email, password)
        devices = client.get_devices_full()
        if args.cmd == "devices":
            out = [
                {k: v for k, v in asdict(d).items() if k not in ("raw", "config")}
                for d in devices
            ]
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0

        if args.cmd == "status":
            targets = [d for d in devices if not args.cid or d.cid == args.cid]
            out = []
            for d in targets:
                status = client.get_airfryer_status(d.cid, d.config_module)
                out.append(
                    {
                        "cid": d.cid,
                        "name": d.name,
                        "is_running": is_airfryer_running(status),
                        "status": status,
                    }
                )
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0

        if args.cmd == "presets":
            targets = [d for d in devices if not args.cid or d.cid == args.cid]
            out = []
            for d in targets:
                presets = client.get_airfryer_presets(d.cid, d.config_module)
                out.append({"cid": d.cid, "name": d.name, "presets": presets.get("menu", presets)})
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0

        if args.cmd == "history":
            targets = [d for d in devices if not args.cid or d.cid == args.cid]
            out = []
            for d in targets:
                if args.all:
                    items = client.get_all_cooking_history(d.cid, d.config_module, page_size=args.limit)
                else:
                    page = client.get_cooking_history(d.cid, d.config_module, page_size=args.limit)
                    items = list(page.get("cookHistoryList") or [])
                out.append({"cid": d.cid, "name": d.name, "count": len(items), "history": items})
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0

        if args.cmd == "monitor":
            targets = [d for d in devices if not args.cid or d.cid == args.cid]
            if not targets:
                print("No matching device found", file=sys.stderr)
                return 1
            dev = targets[0]
            monitor = AirFryerMonitor(
                client,
                dev.cid,
                dev.config_module,
                device_name=dev.name,
                standby_interval=args.standby_interval,
                active_interval=args.active_interval,
                relogin=lambda: client.login(email, password),
            )

            def _on_tick(status: dict, interval: float) -> None:
                print(
                    f"[poll next={interval:.0f}s] status={status.get('cookStatus')} "
                    f"temp={status.get('currentTemp')}{status.get('tempUnit')} "
                    f"rem={status.get('totalTimeRemaining')}s",
                    flush=True,
                )

            def _on_complete(sess: CookSession) -> None:
                payload = {k: v for k, v in asdict(sess).items() if not k.startswith("_")}
                line = json.dumps(payload, ensure_ascii=False)
                print(f"[session_done] {line}", flush=True)
                if args.output:
                    with open(args.output, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")

            try:
                monitor.run(on_tick=_on_tick, on_complete=_on_complete)
            except KeyboardInterrupt:
                return 0
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
