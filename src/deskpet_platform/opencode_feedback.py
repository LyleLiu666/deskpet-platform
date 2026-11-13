"""Install and receive OpenCode's local notification plugin."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile


def plugin_path(command):
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    identity = hashlib.sha256(command.encode()).hexdigest()[:16]
    return root / "opencode" / "plugins" / f"deskpet-{identity}.js"


def configure(path, command, *, enabled):
    template = Path(__file__).with_name("opencode_plugin.js").read_text(encoding="utf-8")
    source = template.replace("/* DESKPET_COMMAND */ []", json.dumps(shlex.split(command)))
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_text(encoding="utf-8") != source)):
        raise ValueError("已有同名 OpenCode 插件，请检查该文件。")
    if path.exists():
        if enabled:
            return False
        path.unlink()
        return True
    if not enabled:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != source:
                    raise ValueError("已有同名 OpenCode 插件，请检查该文件。")
                return False
        finally:
            temporary.unlink(missing_ok=True)
    return True


def deliver(store, event):
    if not isinstance(event, dict):
        return False
    session, identity, kind = event.get("session"), event.get("id"), event.get("kind")
    if not all(isinstance(value, str) and 0 < len(value) <= 100 and value.isprintable()
               for value in (session, identity)):
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
