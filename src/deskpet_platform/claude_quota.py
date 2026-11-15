"""Read Claude Code subscription windows from its documented status-line data."""

def read_statusline_quota(payload, now):
    """Return only valid, unexpired allowance windows understood by the board.

    The status line can omit either window, including for unsupported account
    types or before the first response. Missing fields are never treated as a
    full allowance, and session context usage is deliberately not consulted.
    """
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    limits = limits if isinstance(limits, dict) else {}
    windows = []
    for name, minutes in (("five_hour", 300), ("seven_day", 10080)):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = window.get("used_percentage"), window.get("resets_at")
        if (type(used) in (int, float) and 0 <= used <= 100 and
                type(reset) is int and 1_700_000_000 <= reset <= 0xFFFFFFFF and reset > now):
            windows.append([round(100 - used), minutes, reset])
    return {"quotas": windows}
