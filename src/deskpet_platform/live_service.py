"""Continuous background connection and telemetry service for paired DeskPet boards."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
from typing import Any

from .agent_speed_probe import find_installed_probe
from .device_registry import DeviceRegistry
from .file_lock import acquire
from .notifications import NotificationStore, _run_device_worker
from .quota_capture import QuotaStore


def _resolve_probe(probe_owner: Path, token_probe: Path | str | None, no_token_probe: bool) -> Path | None:
    if no_token_probe:
        return None
    if token_probe is None:
        return find_installed_probe(probe_owner)
    candidate = Path(token_probe).expanduser().resolve()
    if not candidate.is_file():
        raise ValueError("找不到指定的速度采集程序。")
    return candidate


async def run_live_service(
    config_path: Path | str,
    *,
    shared_config: Path | str | None = None,
    token_probe: Path | str | None = None,
    no_token_probe: bool = False,
    usb_port: str | None = None,
    client_factory: Any = None,
    max_seconds: float | None = None,
    poll_interval: float = 0.5,
    handoff_delay: float = 7.0,
) -> None:
    """Keep paired boards connected with live notifications, speed, quota, weather, and computer metrics."""
    config_path = Path(config_path).expanduser().resolve()
    shared_path = Path(shared_config).expanduser().resolve() if shared_config is not None else None
    owner_config = shared_path or config_path
    probe = _resolve_probe(owner_config, token_probe, no_token_probe)

    registry = DeviceRegistry(config_path)
    root = registry.default_path.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)

    service_lock = os.open(root / "live-service.lock", os.O_CREAT | os.O_RDWR, 0o600)
    tasks: dict[Path, asyncio.Task[None]] = {}
    try:
        try:
            acquire(service_lock, blocking=False)
        except BlockingIOError:
            raise RuntimeError("该配置的托肯持续连接服务已在运行。") from None

        shared_inbox = NotificationStore(owner_config.parent / "notifications.sqlite3")
        quota_store = QuotaStore(owner_config.parent / "agent-quota.sqlite3")
        deadline = float("inf") if max_seconds is None else time.monotonic() + max_seconds
        next_scan = 0.0

        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_scan:
                paths = [config_path] if shared_path is not None else await asyncio.to_thread(registry.paths)
                next_scan = now + 5.0
                for index, device_path in enumerate(paths):
                    if device_path in tasks and not tasks[device_path].done():
                        continue
                    is_secondary = shared_path is not None or device_path != registry.default_path
                    source = shared_inbox if is_secondary else None
                    live_options: dict[str, Any] = {
                        "token_probe": probe,
                        "no_token_probe": no_token_probe,
                        "quota_store": quota_store,
                    }
                    if usb_port and index == 0:
                        live_options["usb_port"] = usb_port
                    tasks[device_path] = asyncio.create_task(
                        _run_device_worker(
                            device_path,
                            source=source,
                            client_factory=client_factory,
                            deadline=deadline,
                            poll_interval=poll_interval,
                            handoff_delay=handoff_delay,
                            live_options=live_options,
                        )
                    )
            for device_path, task in list(tasks.items()):
                if task.done():
                    task.result()
                    tasks.pop(device_path, None)
                    next_scan = 0.0
            await asyncio.sleep(poll_interval)
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        os.close(service_lock)
