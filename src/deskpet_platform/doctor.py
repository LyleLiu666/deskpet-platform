"""Inspect setup from a new Agent session without taking the device connection."""
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3

from .agent_speed_probe import find_installed_probe, host_platform
from .codex_hook import (
    EVENT_MATCHERS, CLI_EVENT_MATCHERS, _load, _matches, _previous_installations, _validate_groups, hook_command,
)
from .device_registry import DeviceRegistry
from .notifications import NotificationStore, codex_home


CLIENTS = ("codex", "claude_code", "antigravity", "opencode", "workbuddy", "workbuddy_ai")


def _hook_settings(client, config):
    if client == "codex":
        matchers = tuple(EVENT_MATCHERS.items()) + tuple(CLI_EVENT_MATCHERS.items())
        return codex_home() / "hooks.json", hook_command(config), matchers, True
    if client == "claude_code":
        from .claude_feedback import MATCHERS, settings_path
        return settings_path(), hook_command(config, "claude-hook"), tuple(MATCHERS.items()), False
    if client in ("workbuddy", "workbuddy_ai"):
        from .workbuddy_feedback import MATCHERS, settings_path
        command = hook_command(config, "workbuddy-hook", "--edition", client)
        return settings_path(client), command, tuple(MATCHERS.items()), False
    raise ValueError("unknown JSON hook client")


def _hook_state(client, config):
    if os.name == "nt" and client not in ("codex", "claude_code"):
        return {"status": "unsupported"}
    if os.name == "nt" and not shutil.which("powershell.exe"):
        return {"status": "unavailable", "reason": "powershell_missing"}
    path = None
    previous = False
    try:
        if client == "opencode":
            from .opencode_feedback import _previous_plugins, _read_plugin, plugin_path
            command = hook_command(config, "opencode-hook")
            path = plugin_path(command)
            plugin = _read_plugin(path)
            expected = b"const command = " + json.dumps(shlex.split(command)).encode() + b";"
            state = "missing" if plugin is None else "written" if expected in plugin.splitlines() else "invalid"
            previous = next(_previous_plugins(command), None) is not None
        elif client == "antigravity":
            from .antigravity_feedback import _entry_name, _previous_entries, hook_entry, settings_path
            path = settings_path()
            command = hook_command(config, "antigravity-hook")
            data = _load(path, private=False)[0] if path.exists() or path.is_symlink() else {}
            entry = data.get(_entry_name(command))
            previous = any(data[name].get("enabled") is not False for name in _previous_entries(data, command))
            if entry is None:
                state = "missing"
            elif not isinstance(entry, dict):
                state = "invalid"
            elif entry.get("enabled") is False:
                state = "disabled"
            else:
                expected = hook_entry(command)
                installed = sum(entry.get(event) == groups for event, groups in expected.items())
                state = "written" if installed == len(expected) else "partial" if installed else "invalid"
        else:
            path, command, matchers, private = _hook_settings(client, config)
            data = _load(path, private=private)[0] if path.exists() or path.is_symlink() else {}
            installed = 0
            for event, matcher in matchers:
                groups = data.get("hooks", {}).get(event, [])
                _validate_groups(groups, event)
                installed += any(group.get("matcher") == matcher and any(_matches(h, command) for h in group["hooks"])
                                 for group in groups)
            state = "written" if installed == len(matchers) else "partial" if installed else "missing"
            previous = bool(_previous_installations(data, command, (), (event for event, _ in matchers)))
    except (OSError, ValueError):
        return {"status": "invalid", "settings": str(path) if path is not None else None}
    if previous and state != "invalid":
        return {"status": "needs_reinstall", "settings": str(path)}
    return {"status": state, "settings": str(path)}


def _open_inbox(config):
    path = NotificationStore(config.parent / "notifications.sqlite3").path
    if path.is_symlink() or not path.is_file():
        return None
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("notification_store_requires_private_permissions")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)


def _event_progress(shared, selected, source):
    incoming = _open_inbox(shared)
    if incoming is None:
        return {"completed": None, "response": None}
    with closing(incoming) as inbox:
        rows = {category: inbox.execute(
            "SELECT session,turn,created FROM notifications WHERE source=? AND category=? ORDER BY id DESC LIMIT 1",
            (source, category)).fetchone() for category in ("completed", "response")}
    receipts = _open_inbox(selected) if selected is not None else None
    displayed = {}
    if receipts is not None:
        with closing(receipts) as board:
            has_receipts = board.execute(
                "SELECT 1 FROM pragma_table_info('notifications') WHERE name='displayed'").fetchone()
            if has_receipts:
                for category, row in rows.items():
                    if row is not None:
                        result = board.execute("SELECT displayed FROM notifications WHERE source=? AND session=? AND turn=?",
                                               (source, row[0], row[1])).fetchone()
                        displayed[category] = result[0] if result else None
    return {category: {"hook_triggered_at": row[2], "device_displayed_at": displayed.get(category)} if row else None
            for category, row in rows.items()}


def diagnose(config, *, client, device_id=None, shared_config=None, token_probe=None, no_token_probe=False):
    registry = DeviceRegistry(config)
    devices = registry.list() if config.exists() or config.is_symlink() or registry.root.exists() else []
    if device_id is None and len(devices) == 1:
        device_id = devices[0]["id"]
    selected = registry.path_for(device_id) if device_id is not None else None
    shared = shared_config or config
    try:
        source = "workbuddy" if client == "workbuddy_ai" else client
        events = _event_progress(shared, selected, source)
        if client in ("workbuddy", "workbuddy_ai"):
            # Both editions record the same source name in existing databases.
            events["source_scope"] = "both_workbuddy_editions"
    except (OSError, sqlite3.Error, ValueError):
        events = {"status": "unavailable", "reason": "notification_store_unreadable"}
    probe = None if no_token_probe else token_probe or find_installed_probe(shared)
    probe_status = "disabled" if no_token_probe else "missing"
    if probe is not None:
        runnable = probe.is_file() and (os.name == "nt" or os.access(probe, os.X_OK))
        probe_status = "present" if runnable else "invalid_path"
    return {
        "host": host_platform(),
        "pairing": {"devices": devices, "selected_device_id": device_id, "connection": "not_checked"},
        "reminders": {"client": client, "hook": _hook_state(client, shared), "latest_events": events},
        "speed": {"status": probe_status, "path": str(probe) if probe else None, "reading": "not_checked"},
    }
