"""Add quota capture to a local Agent status line while preserving its command.

The user settings remain the source of truth and contain enough information
to undo precisely this installation. Windows support is being rehearsed with
an explicitly selected shell; automatic selection is not enabled yet.
"""

import base64
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile

from .file_lock import acquire
from .quota_capture import READERS


MARKER = ": deskpet-quota-v1-"


def settings_path(source):
    if source == "claude_code":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"
    if source == "antigravity":
        return Path.home() / ".gemini/antigravity-cli/settings.json"
    raise ValueError("不支持的额度来源。")


def _command(metadata):
    if "windows_shell" in metadata:
        from .quota_windows_statusline import command
        return command(metadata)
    encoded = base64.urlsafe_b64encode(json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
                                       .encode()).decode().rstrip("=")
    capture = shlex.join([metadata["executable"], "-m", "deskpet_platform.quota_capture",
                          metadata["source"], metadata["store"]])
    # If the Python installation is absent, the original status line still
    # receives its stdin.  Grouping preserves compound commands and comments.
    if metadata["original_command"]:
        body = f"({capture} 2>/dev/null || cat) | (\n{metadata['original_command']}\n)"
    else:
        body = f"({capture} >/dev/null 2>&1 || cat >/dev/null)"
    return MARKER + encoded + "\n" + body


def _metadata(command):
    if not isinstance(command, str):
        return None
    from .quota_windows_statusline import PREFIX, metadata as windows_metadata
    is_windows = command.startswith(PREFIX)
    if is_windows:
        data = windows_metadata(command)
        if data is None:
            return None
    elif command.startswith(MARKER):
        token = command[len(MARKER):].partition("\n")[0]
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            data = json.loads(raw)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("DeskPet 额度状态栏配置已被修改，请先检查原有设置。") from None
    else:
        return None
    try:
        required = {"source", "store", "executable", "original_command", "had_status", "added_type", "added_stack"}
        if not isinstance(data, dict) or set(data) not in (required, required | {"windows_shell"}):
            raise ValueError
        if (data["source"] not in READERS or not all(isinstance(data[k], str) for k in ("store", "executable")) or
                not (data["original_command"] is None or isinstance(data["original_command"], str)) or
                any(type(data[k]) is not bool for k in ("had_status", "added_type", "added_stack"))):
            raise ValueError
        if is_windows != ("windows_shell" in data):
            raise ValueError
        if is_windows:
            shell = data["windows_shell"]
            if (not isinstance(shell, list) or len(shell) != 2 or shell[0] not in ("bash", "powershell", "cmd") or
                    not isinstance(shell[1], str) or not shell[1]):
                raise ValueError
        if not is_windows and _command(data) != command:
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("DeskPet 额度状态栏配置已被修改，请先检查原有设置。") from None


def _read(path):
    if path.is_symlink():
        raise ValueError("Agent 设置文件是符号链接，未修改。")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    try:
        settings = json.loads(raw)
        if not isinstance(settings, dict):
            raise ValueError
        return settings, raw
    except (ValueError, UnicodeError):
        raise ValueError("Agent 设置文件无法解析，未修改。") from None


def _write(path, settings, original):
    previous_mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            if os.name != "nt":
                os.fchmod(stream.fileno(), previous_mode)
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("Agent 设置文件已改变，未修改。")
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != original:
            raise ValueError("Agent 设置文件刚被其他程序修改，请重试。")
        if os.name == "nt" and original is not None:
            from .quota_windows_statusline import replace_file
            replace_file(path, temporary)
        elif os.name == "nt":
            # On Windows rename fails rather than replacing a newly created file.
            os.rename(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def configure(path, source, store, *, enabled, executable=None, windows_shell=None):
    """Install or remove our status line command; return whether settings changed.

    The caller must use the Agent's actual user settings path.  Project or
    administrator settings may override these settings and require verification.
    """
    if os.name == "nt" and windows_shell is None:
        raise ValueError("Windows 状态栏接入尚未验证，未修改 Agent 设置。")
    if windows_shell is not None and (os.name != "nt" or not isinstance(windows_shell, (tuple, list)) or
            len(windows_shell) != 2 or windows_shell[0] not in ("bash", "powershell", "cmd") or
            not isinstance(windows_shell[1], str) or not windows_shell[1]):
        raise ValueError("Windows 状态栏命令环境无效，未修改 Agent 设置。")
    if source not in READERS:
        raise ValueError("不支持的额度来源。")
    path, store = Path(path), Path(store).expanduser().resolve()
    if not enabled and not path.exists() and not path.is_symlink():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.with_name(f".{path.name}.deskpet-quota.lock"), flags, 0o600)
    try:
        acquire(fd)
        settings, raw = _read(path)
        status = settings.get("statusLine")
        if status is not None and not isinstance(status, dict):
            raise ValueError("Agent 状态栏设置格式不支持，未修改。")
        current = status.get("command") if status is not None else None
        previous = _metadata(current)
        if previous and previous["source"] != source:
            raise ValueError("该状态栏属于另一个额度来源，未修改。")
        if not enabled:
            if previous is None:
                return False
            if previous["original_command"] is None:
                status.pop("command", None)
            else:
                status["command"] = previous["original_command"]
            if previous["added_type"] and status.get("type") == "command":
                status.pop("type")
            if previous["added_stack"] and status.get("stack_with_default") is True:
                status.pop("stack_with_default")
            if not previous["had_status"] and not status:
                settings.pop("statusLine")
            _write(path, settings, raw)
            return True
        if status is not None:
            if status.get("enabled") is False or status.get("type", "command") != "command":
                raise ValueError("Agent 状态栏已关闭或格式不支持，未修改。")
            if current is not None and not isinstance(current, str):
                raise ValueError("Agent 状态栏命令无效，未修改。")
        if previous:
            metadata = {**previous, "store": str(store), "executable": str(executable or sys.executable)}
        else:
            metadata = {"source": source, "store": str(store), "executable": str(executable or sys.executable),
                        "original_command": current, "had_status": status is not None,
                        "added_type": status is None or "type" not in status,
                        "added_stack": source == "antigravity" and status is None}
        if windows_shell is not None:
            metadata["windows_shell"] = list(windows_shell)
        command = _command(metadata)
        if previous and current == command:
            return False
        if status is None:
            status = settings["statusLine"] = {}
        if metadata["added_type"]:
            status["type"] = "command"
        if metadata["added_stack"]:
            status["stack_with_default"] = True
        status["command"] = command
        _write(path, settings, raw)
        return True
    finally:
        os.close(fd)
