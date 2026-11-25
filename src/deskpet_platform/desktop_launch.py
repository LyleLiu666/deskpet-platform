"""Open the desktop target associated with a notification."""
import os
import subprocess
import sys
from uuid import UUID


def supports_desktop_launch():
    return sys.platform == "darwin" or os.name == "nt"


def _codex_session_url(session):
    try:
        thread = UUID(session)
    except (ValueError, AttributeError, TypeError):
        return None
    return f"codex://threads/{thread}" if str(thread) == session else None


def notification_launch_args(source, session, *, exact=False):
    if source == "antigravity" and sys.platform == "darwin" and not exact:
        return ("-b", "com.google.antigravity")
    if source == "codex" and (url := _codex_session_url(session)):
        return (url,)
    return None


def launch_notification(args):
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", *args], check=True, timeout=2,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        os.startfile(args[0])
    else:
        return False
    return True
