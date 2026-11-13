"""Install and receive local Codex hooks without replacing existing hooks."""
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile

from .notifications import NotificationStore
from . import codex_feedback
from .device_registry import DeviceRegistry


EVENT_MATCHERS = {
    "Stop": None,
    "PreToolUse": codex_feedback.TOOL,
    "PostToolUse": codex_feedback.TOOL,
    "UserPromptSubmit": None,
}


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def hook_command(config, hook="codex-hook", *arguments):
    return shlex.join([sys.executable, "-m", "deskpet_platform", "--config", str(config), hook, *arguments])


def notification_config(config):
    """Keep already trusted per-board commands working after the upgrade."""
    config = Path(config)
    directory = config.parent
    if directory.parent.name == "devices" and config.name == "device.json":
        default = directory.parent.parent / "device.json"
        if config in DeviceRegistry(default).paths():
            return default
    return config


def _load(path, *, private=True):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Hooks 设置文件不能是符号链接。")
    if path.exists():
        allowed_mode = 0o600 if private else 0o644
        if stat.S_IMODE(path.stat().st_mode) & ~allowed_mode:
            raise ValueError("Hooks 设置文件权限过宽，请检查文件权限。")
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("Hooks 设置文件无法解析，未修改原文件。") from error
    else:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        raise ValueError("Hooks 设置文件结构无效，未修改原文件。")
    return config


def _write(path, config, *, private=True):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            if not private and path.exists():
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def install_hook(path, command, *, matchers=EVENT_MATCHERS, private=True):
    path = Path(path)
    config = _load(path, private=private)
    hooks = config.setdefault("hooks", {})
    added = False
    for event, matcher in matchers.items():
        existing = hooks.setdefault(event, [])
        _validate_groups(existing, event)
        if any(group.get("matcher") == matcher and any(_matches(hook, command) for hook in group["hooks"])
               for group in existing):
            continue
        group = {"hooks": [{"type": "command", "command": command, "timeout": 2}]}
        if matcher is not None:
            group["matcher"] = matcher
        existing.append(group)
        added = True
    if added:
        _write(path, config, private=private)
    return added


def _validate_groups(existing, event):
    if not isinstance(existing, list):
        raise ValueError(f"{event} hooks 结构无效，未修改原文件。")
    for group in existing:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError(f"{event} hooks 结构无效，未修改原文件。")


def _matches(hook, command):
    return isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command


def remove_hook(path, command, *, matchers=EVENT_MATCHERS, private=True):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return False
    config = _load(path, private=private)
    hooks = config.get("hooks", {})
    removed = False
    for event in matchers:
        existing = hooks.get(event, [])
        _validate_groups(existing, event)
        updated, event_removed = _remove_groups(existing, command)
        if event_removed:
            if updated:
                hooks[event] = updated
            else:
                del hooks[event]
            removed = True
    if removed:
        _write(path, config, private=private)
    return removed


def _remove_groups(existing, command):
    updated, removed = [], False
    for index, group in enumerate(existing):
        remaining, group_removed = [], False
        for hook_index, hook in enumerate(group["hooks"]):
            if _matches(hook, command):
                if hook_index < len(group["hooks"]) - 1:
                    raise ValueError("该 hook 后面还有其他同组 hook。请在 Agent 设置中手动移除，保留其他条目。")
                removed = True
                group_removed = True
            else:
                remaining.append(hook)
        if not group_removed:
            updated.append(group)
        elif remaining or index < len(existing) - 1:
            updated.append({**group, "hooks": remaining})
    return updated, removed


def receive(config, *, handle=None, response=None):
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("hook_event_too_large")
        event = json.loads(raw)
        config = notification_config(config)
        store = NotificationStore(config.parent / "notifications.sqlite3")
        if handle is not None:
            changed = handle(store, event)
        elif event.get("hook_event_name") == "Stop":
            changed = store.add_codex_event(event)
        else:
            changed = codex_feedback.deliver(store, event)
        if changed:
            subprocess.Popen([sys.executable, "-m", "deskpet_platform", "--config", str(config), "notification-worker"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    except Exception:
        pass
    print(json.dumps(response or {}))
