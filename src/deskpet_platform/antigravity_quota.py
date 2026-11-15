"""Read quota buckets from Antigravity CLI's documented status-line data."""

from datetime import datetime


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
