"""Install and receive OpenCode's local notification plugin."""
from errno import ELOOP
import hashlib
import json
import os
from pathlib import Path

from .codex_hook import generated_hook_target
from .file_lock import acquire
import shlex
import stat
import tempfile


COMMAND_MARKER = b"/* DESKPET_COMMAND */ []"
PREVIOUS_PLUGIN_SHA256 = "0f4e225d4198dca74c915b802692c86e65f3bce169527b1b3ca822ebcdf06577"
PLUGIN_CONFLICT = "已有同名 OpenCode 插件，请检查该文件。"


def plugin_path(command):
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    identity = hashlib.sha256(command.encode()).hexdigest()[:16]
    return root / "opencode" / "plugins" / f"deskpet-{identity}.js"


def configure(path, command, *, enabled):
    if not enabled and not path.exists() and not path.is_symlink():
        return False
    arguments = json.dumps(shlex.split(command)).encode()
    template = Path(__file__).with_name("opencode_plugin.js").read_bytes()
    source = template.replace(COMMAND_MARKER, arguments, 1)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(path.with_name(f".{path.name}.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        acquire(lock)
        previous = _read_plugin(path)
        if previous is not None and previous != source:
            installed_command = b"const command = " + arguments + b";\n"
            original = previous.replace(installed_command, b"const command = " + COMMAND_MARKER + b";\n", 1)
            if original == previous or hashlib.sha256(original).hexdigest() != PREVIOUS_PLUGIN_SHA256:
                raise ValueError(PLUGIN_CONFLICT)
        if not enabled:
            if previous is None:
                return False
            if _read_plugin(path) != previous:
                raise ValueError(PLUGIN_CONFLICT)
            path.unlink()
        elif previous == source:
            return False
        else:
            return _write_plugin(path, source, previous)
    finally:
        os.close(lock)
    return True


def _installed_command(path, template):
    try:
        data = _read_plugin(path)
        if data is None:
            return None
        line = next(row for row in data.splitlines(keepends=True) if row.startswith(b"const command = "))
        arguments = json.loads(line.removeprefix(b"const command = ").removesuffix(b";\n"))
        if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
            return None
        command = shlex.join(arguments)
        pristine = data.replace(line, b"const command = " + COMMAND_MARKER + b";\n", 1)
        if path == plugin_path(command) and (pristine == template or
                hashlib.sha256(pristine).hexdigest() == PREVIOUS_PLUGIN_SHA256):
            return command
    except (OSError, ValueError, StopIteration):
        pass
    return None


def configure_registry(command, previous_commands, *, enabled):
    path = plugin_path(command)
    if not enabled and not path.parent.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(path.parent / ".deskpet-plugins.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        acquire(lock)
        targets = {generated_hook_target(item) for item in (command, *previous_commands)} - {None}
        obsolete = set(previous_commands)
        template = Path(__file__).with_name("opencode_plugin.js").read_bytes()
        for existing in path.parent.glob("deskpet-*.js"):
            if existing == path:
                continue
            installed = _installed_command(existing, template)
            if generated_hook_target(installed) in targets:
                obsolete.add(installed)
        changed = configure(path, command, enabled=enabled)
        for old in sorted(obsolete - {command}):
            changed = configure(plugin_path(old), old, enabled=False) or changed
        return changed
    finally:
        os.close(lock)


def _read_plugin(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == ELOOP:
            raise ValueError(PLUGIN_CONFLICT) from error
        raise
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(PLUGIN_CONFLICT)
        return stream.read()


def _write_plugin(path, source, previous):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
            if previous is None:
                # Never overwrite a new file during initial install.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if _read_plugin(path) == source:
                        return False
                    raise ValueError(PLUGIN_CONFLICT) from None
            elif _read_plugin(path) == previous:
                os.replace(temporary, path)
            else:
                raise ValueError(PLUGIN_CONFLICT)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def deliver(store, event):
    if not isinstance(event, dict):
        return False
    session, identity, kind = event.get("session"), event.get("id"), event.get("kind")
    if not isinstance(session, str) or not 0 < len(session) <= 100 or not session.isprintable():
        return False
    if kind == "session-ended":
        return bool(store.resolve_session_requests("opencode", session))
    if not isinstance(identity, str) or not 0 < len(identity) <= 100 or not identity.isprintable():
        return False
    if kind == "completed":
        category, prefix, default = "completed", "message", "OpenCode"
    else:
        prefix = event.get("request")
        if prefix not in ("question", "permission"):
            return False
        if kind == "resolved":
            return bool(store.resolve_agent_event("opencode", session, f"{prefix}-{identity}"))
        if kind != "asked":
            return False
        category = "response"
        default = "OpenCode 提问" if prefix == "question" else "OpenCode 请求授权"
    title = event.get("title")
    if not isinstance(title, str) or not title.strip() or not title.isprintable():
        title = default
    return bool(store.add_agent_event("opencode", session, f"{prefix}-{identity}", category, title))
