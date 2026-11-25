"""Send canonical history snapshots over the paired DeskPet channel."""
import asyncio
import time

from .analytics_store import SOURCES


class AnalyticsSync:
    def __init__(self, store, notifications, *, device=None):
        self.store = store
        self.notifications = notifications
        self.device = device
        self.epoch = None
        self.confirmed = {}
        self.next_history = 0.0
        self.last_synced_at = None

    async def step(self, client, *, quota_store, codex=None, now=None):
        channel = client.channel
        if not channel.online or channel.capabilities.get("agent_analytics") != 1:
            return
        now = time.time() if now is None else now
        if self.epoch != channel.epoch:
            self.confirmed.clear()
            self.epoch = channel.epoch
            self.next_history = 0
        attention = await asyncio.to_thread(self.notifications.analytics_attention)
        await channel.send("analytics_attention", rows=attention)
        if now < self.next_history:
            return
        await asyncio.to_thread(self.store.collect, now)
        await asyncio.to_thread(self.store.collect_quota, quota_store, codex=codex)
        board = await channel.send("analytics_state")
        seen = set()
        for state in board["sources"]:
            source = state["source"]
            if type(source) is not int or not 0 <= source < len(SOURCES) or source in seen:
                raise ValueError("invalid_analytics_state")
            seen.add(source)
            payload = await asyncio.to_thread(self.store.compute_source_payload, source, now)
            await self._sync_source(channel, source, state, payload)
        if seen != set(range(len(SOURCES))):
            raise ValueError("invalid_analytics_state")
        self.next_history = now + 15
        self.last_synced_at = int(time.time())
        if self.device is not None:
            await asyncio.to_thread(self.store.record_sync, self.device, self.last_synced_at)

    async def _sync_source(self, channel, source, state, payload):
        rich_usage = "usage_crc" in state
        usage_changed = rich_usage and state["usage_crc"] != payload["usage_crc"]
        quota_changed = state["quota_crc"] != payload["quota_crc"]
        rollup_changed = state["rollup_crc"] != payload["rollup_crc"]
        if rich_usage:
            days = [(*row, *payload["day_usage"][row[0]]) if payload["day_usage"][row[0]][0] is not None else row
                    for row in payload["days"]]
            sessions = [dict(row, inputs=payload["session_usage"][row["key"]])
                        if payload["session_usage"][row["key"]][0] is not None else row
                        for row in payload["sessions"]]
            payload = dict(payload, days=days, sessions=sessions)
        if not quota_changed and not rollup_changed and not usage_changed:
            self.confirmed[source] = payload
            return

        crcs = {key: payload[key] for key in ("quota_crc", "rollup_crc")}
        if rich_usage:
            crcs["usage_crc"] = payload["usage_crc"]
        if state.get("unit_kind") == payload["unit_kind"]:
            previous = self.confirmed.get(source)
            if previous and (previous["rollup_crc"] != state["rollup_crc"] or
                             (rich_usage and previous["usage_crc"] != state["usage_crc"])):
                previous = None
            known_days = {row[0]: row for row in previous["days"]} if previous else {}
            known_sessions = {row["key"]: row for row in previous["sessions"]} if previous else {}
            days, sessions = [], []
            if rollup_changed or usage_changed:
                # A cache-only correction can affect any past day without
                # moving the board's last synced date.
                backfill = previous is not None or (usage_changed and not rollup_changed)
                days = [row for row in payload["days"] if (backfill or row[0] >= state["last_synced_day"])
                        and known_days.get(row[0]) != row]
                sessions = [row for row in payload["sessions"] if known_sessions.get(row["key"]) != row]
            args = {"quota": payload["quota"]} if quota_changed else {}
            if await self._patch_and_commit(channel, source, args, days, sessions, crcs):
                self.confirmed[source] = payload
                return

        args = {"reset": True, "unit_kind": payload["unit_kind"], "quota": payload["quota"]}
        if not await self._patch_and_commit(channel, source, args, payload["days"], payload["sessions"], crcs):
            raise ValueError(f"analytics_resync_failed:{SOURCES[source]}")
        self.confirmed[source] = payload

    @staticmethod
    async def _patch_and_commit(channel, source, fields, days, sessions, crcs):
        packet = dict(source=source, **fields)
        for key, rows in (("days", days), ("sessions", sessions)):
            for row in rows:
                candidate = {**packet, key: [*packet.get(key, ()), row]}
                if not channel.fits_request("analytics_patch", **candidate):
                    await channel.send("analytics_patch", **packet)
                    candidate = dict(source=source, **{key: [row]})
                    if not channel.fits_request("analytics_patch", **candidate):
                        raise ValueError("analytics_row_too_long")
                packet = candidate
        if len(packet) > 1:
            await channel.send("analytics_patch", **packet)
        response = await channel.send("analytics_commit", source=source, **crcs)
        return response["need_resync"] is False
