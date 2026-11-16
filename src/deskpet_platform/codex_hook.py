"""Install and receive local Codex hooks without replacing existing hooks."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys
import tempfile

from .file_lock import acquire
from .notifications import NotificationStore
from . import codex_feedback
from .device_registry import DeviceRegistry


EVENT_MATCHERS = {
    "Stop": None,
    "PreToolUse": codex_feedback.TOOL,
    "PostToolUse": codex_feedback.TOOL,
    "UserPromptSubmit": None,
}
CLI_EVENT_MATCHERS = {event: f"^{codex_feedback.CLI_TOOL}$" for event in ("PreToolUse", "PostToolUse")}


def hook_command(config, hook="codex-hook", *arguments):
    if os.name == "nt" and hook in ("claude-hook", "codex-hook"):
        from .windows_hook_command import build
        return build(sys.executable, ["-m", "deskpet_platform", "--config", str(config), hook, *arguments])
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
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & ~allowed_mode:
            raise ValueError("Hooks 设置文件权限过宽，请检查文件权限。")
        try:
            original = path.read_bytes()
            config = json.loads(original)
        except (OSError, ValueError) as error:
            raise ValueError("Hooks 设置文件无法解析，未修改原文件。") from error
    else:
        config = {}; original = None
    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        raise ValueError("Hooks 设置文件结构无效，未修改原文件。")
    return config, original


def _write(path, config, *, original, private=True):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            if os.name != "nt" and not private and path.exists():
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("Hooks 设置文件已改变，未修改。")
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != original:
            raise ValueError("Hooks 设置文件刚被其他程序修改，请重试。")
        if os.name == "nt" and original is not None:
            from .windows_private import replace_file
            replace_file(path, temporary)
        elif os.name == "nt":
            # Do not replace a file created by another program meanwhile.
            os.rename(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_hook(path, command, *, matchers=EVENT_MATCHERS, private=True):
    path = Path(path)
    config, original = _load(path, private=private)
    added = _add_to_config(config, command, matchers)
    if added:
        _write(path, config, original=original, private=private)
    return added


def _add_to_config(config, command, matchers):
    hooks = config.setdefault("hooks", {})
    added = False
    entries = tuple(matchers.items())
    if matchers is EVENT_MATCHERS:
        entries += tuple(CLI_EVENT_MATCHERS.items())
    for event, matcher in entries:
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
    config, original = _load(path, private=private)
    removed = _remove_from_config(config, command, matchers)
    if removed:
        _write(path, config, original=original, private=private)
    return removed


def _remove_from_config(config, command, matchers):
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
    return removed


def generated_hook_target(command):
    if not isinstance(command, str):
        return None
    if command.startswith("powershell.exe "):
        from .windows_hook_command import generated_arguments
        arguments = generated_arguments(command)
        return ("windows", arguments) if arguments is not None else None
    try:
        executable, *arguments = shlex.split(command)
    except ValueError:
        return None
    python = PurePosixPath(executable)
    if (python.is_absolute() and re.fullmatch(r"python(?:3(?:\.[0-9]+t?)?)?", python.name)
            and arguments[:3] == ["-m", "deskpet_platform", "--config"]
            and len(arguments) >= 5 and shlex.join([executable, *arguments]) == command):
        return ("posix", tuple(arguments))
    return None


def _previous_installations(config, command, previous_commands, matchers):
    previous_commands = tuple(previous_commands)
    targets = {generated_hook_target(item) for item in (command, *previous_commands)} - {None}
    if not targets:
        return previous_commands
    matches = list(previous_commands)
    for event in matchers:
        groups = config.get("hooks", {}).get(event, [])
        _validate_groups(groups, event)
        for group in groups:
            for hook in group["hooks"]:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    continue
                existing = hook.get("command")
                if existing != command and generated_hook_target(existing) in targets and existing not in matches:
                    matches.append(existing)
    return matches


def configure_hooks(path, command, previous_commands, *, enabled, matchers, private):
    """Publish a single validated update for the shared command and all old commands."""
    path = Path(path)
    if not enabled and not path.exists() and not path.is_symlink():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.with_name(f".{path.name}.deskpet.lock"), flags, 0o600)
    try:
        acquire(descriptor)
        config, original = _load(path, private=private)
        changed = (_add_to_config(config, command, matchers) if enabled else
                   _remove_from_config(config, command, matchers))
        for previous in _previous_installations(config, command, previous_commands, matchers):
            changed = _remove_from_config(config, previous, matchers) or changed
        if changed:
            _write(path, config, original=original, private=private)
        return changed
    finally:
        os.close(descriptor)


def configure_codex_hooks(path, command, previous_commands, *, enabled):
    return configure_hooks(path, command, previous_commands, enabled=enabled, matchers=EVENT_MATCHERS, private=True)


def _remove_groups(existing, command):
    updated, removed = [], False
    # Remove matching suffix groups entirely, but reserve any slot before
    # a surviving group so its Codex trust key keeps the same index.
    for group in reversed(existing):
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
        elif remaining or updated:
            updated.append({**group, "hooks": remaining})
    return list(reversed(updated)), removed


def receive_event(config, event, *, handle=None):
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
        receivers=config.parent / "receivers"
        if not receivers.is_symlink() and receivers.is_dir():
            subprocess.Popen([sys.executable, "-m", "deskpet_platform", "--config", str(config), "receiver-worker"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)


def receive(config, *, handle=None, response=None):
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("hook_event_too_large")
        receive_event(config, json.loads(raw), handle=handle)
    except Exception:
        pass
    print(json.dumps(response or {}))
