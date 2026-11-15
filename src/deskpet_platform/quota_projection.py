"""Project one selected agent's independently reported allowance for a board."""


SOURCES = ("codex", "claude_code", "antigravity_gemini", "antigravity_third_party")

# These are provider bucket identifiers, not model-name or prompt heuristics.
# Other or newly introduced buckets stay separate until their meaning is known.
ANTIGRAVITY_WINDOWS = {
    "antigravity_gemini": (("gemini-5h", 300), ("gemini-weekly", 10080)),
    "antigravity_third_party": (("3p-5h", 300), ("3p-weekly", 10080)),
}


def _valid_window(row, now):
    if not isinstance(row, (list, tuple)) or len(row) != 3:
        return False
    remaining, minutes, reset = row
    return (type(remaining) is int and 0 <= remaining <= 100 and
            type(minutes) is int and 0 < minutes <= 525600 and
            (reset is None or type(reset) is int and now < reset <= 0xFFFFFFFF))


def project_quota(source, *, codex=None, claude=None, antigravity=None, now):
    if source not in SOURCES:
        raise ValueError("unknown_quota_source")
    result = {"source": source, "quotas": []}
    if source in ANTIGRAVITY_WINDOWS:
        values = antigravity if isinstance(antigravity, dict) else {}
        for identity, minutes in ANTIGRAVITY_WINDOWS[source]:
            bucket = values.get(identity)
            if isinstance(bucket, dict):
                row = [bucket.get("remaining_percent"), minutes, bucket.get("reset_at")]
                if _valid_window(row, now):
                    result["quotas"].append(row)
        return result
    values = codex if source == "codex" else claude
    if not isinstance(values, dict):
        return result
    rows = values.get("quotas")
    if isinstance(rows, list):
        seen = set()
        for row in rows:
            if _valid_window(row, now) and row[1] not in seen:
                result["quotas"].append(list(row))
                seen.add(row[1])
                if len(result["quotas"]) == 2:
                    break
    if source == "codex":
        for key in ("credits", "reset_cards"):
            if key in values:
                result[key] = values[key]
    return result
