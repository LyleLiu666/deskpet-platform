"""Read quota buckets from Antigravity CLI's documented status-line data."""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def _reset_time(bucket, now):
    value = bucket.get("reset_time")
    if value is not None:
        if not isinstance(value, str) or len(value) > 40:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                return False
            reset = int(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            return False
    else:
        relative = bucket.get("reset_in_seconds")
        if relative is None:
            return None
        if type(relative) not in (int, float) or not 0 < relative <= 0xFFFFFFFF - now:
            return False
        reset = int(now + relative)
    return reset if now < reset <= 0xFFFFFFFF else False


def read_statusline_quota(payload, now):
    """Keep quota bucket IDs and values without guessing their meaning.

    A count is not a percentage, and a missing reset time is not inferred from
    a bucket name. Account, transcript, model and conversation fields are not
    returned. Call this at capture time so relative reset times do not slide.
    """
    quotas = payload.get("quota") if isinstance(payload, dict) else None
    if not isinstance(quotas, dict):
        return {}
    result = {}
    for identity, bucket in quotas.items():
        if (not isinstance(identity, str) or not 0 < len(identity) <= 128 or not identity.isprintable() or
                not isinstance(bucket, dict) or bucket.get("disabled", False) is not False):
            continue
        reset = _reset_time(bucket, now)
        if reset is False:
            continue
        fraction, amount = bucket.get("remaining_fraction"), bucket.get("remaining_amount")
        if type(fraction) in (int, float) and 0 <= fraction <= 1:
            value = {"remaining_percent": round(fraction * 100)}
        elif type(amount) in (int, float) and 0 <= amount <= 0x1FFFFFFFFFFFFF:
            value = {"remaining_amount": amount}
        else:
            continue
        result[identity] = {**value, "reset_at": reset}
    return result


def read_usage_buckets(payload):
    """Extract the quota bucket IDs from agy's own noninteractive /usage result."""
    if (not isinstance(payload, dict) or payload.get("status") != "SUCCESS" or
            type(payload.get("num_turns")) is not int or payload["num_turns"] != 0):
        return {}
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return {}
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return {}

    buckets = {}
    for group in groups:
        rows = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            identity = row.get("id") if isinstance(row, dict) else None
            if isinstance(identity, str):
                if identity in buckets:
                    return {}
                buckets[identity] = row
    return buckets


def fetch_usage_buckets():
    """Ask the installed official CLI to read quota using its existing sign-in."""
    executable = shutil.which("agy")
    if executable is None:
        default = Path.home() / ".local/bin/agy"
        if not default.is_file():
            return {}
        executable = str(default)
    try:
        with tempfile.TemporaryDirectory(prefix="deskpet-agy-quota-") as directory:
            result = subprocess.run(
                [executable, "--print", "/usage", "--output-format", "json",
                 "--print-timeout", "15s", "--log-file", os.devnull],
                cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=25, check=True,
                env={**os.environ, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
            )
        return read_usage_buckets(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
