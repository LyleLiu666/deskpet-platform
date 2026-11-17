"""Open an existing Codex session on the paired computer."""
import os
import subprocess
import sys
from uuid import UUID


def can_open_codex_session():
    return sys.platform == "darwin" or os.name == "nt"


def codex_session_url(session):
    try:
        thread = UUID(session)
    except (ValueError, AttributeError, TypeError):
        return None
    return f"codex://threads/{thread}" if str(thread) == session else None


def open_codex_session(url):
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", url], check=True, timeout=2,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        os.startfile(url)
    else:
        return False
    return True
