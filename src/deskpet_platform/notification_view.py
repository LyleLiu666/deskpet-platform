"""Project per-device notifications onto the original scene's screen protocol."""
from datetime import datetime
import json
import zlib

from PIL import Image, ImageDraw

from .client import _text_font
from .notification_graphics import home_lines, notification_row
from .notifications import SOURCE_NAMES


def _detail_pixels(item):
    image = Image.new("1", (288, 112))
    draw = ImageDraw.Draw(image)
    font = _text_font(16)
    title = item["title"]
    lines = []
    while title and len(lines) < 2:
        end = 1
        while end < len(title) and draw.textlength(title[:end + 1], font=font) <= 288:
            end += 1
        lines.append(title[:end])
        title = title[end:]
    if title and lines:
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > 288:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    for index, line in enumerate(lines):
        draw.text((0, index * 20), line, font=font, fill=1)
    source = SOURCE_NAMES.get(item["source"], item["source"])
    state = ("已应答" if item["resolved"] else "需要应答") if item["category"] == "response" else "已完成"
    draw.text((0, 50), f"{source}  {state}", font=_text_font(14), fill=1)
    draw.text((0, 70), "请在电脑上查看", font=_text_font(14), fill=1)
    return image.tobytes()


def _cached_home_summary(records, *, unread_responses=False):
    completed_sessions = set()
    completed_sources = {}
    responses = {}
    active = []
    for item in records:
        source = item["source"]
        if item["category"] == "completed" and not item["seen"]:
            completed_sessions.add((source, item["completion_session"]))
            completed_sources[source] = None
            active.append(item["id"])
        elif item["category"] == "response" and not item["resolved"] and (not unread_responses or not item["seen"]):
            entry = responses.setdefault(source, {"source": source, "count": 0,
                                                   "id": item["id"], "title": item["title"]})
            entry["count"] += 1
            active.append(item["id"])
    return {"completed_count": len(completed_sessions), "completed_sources": list(completed_sources),
            "response_source_count": len(responses), "response_count": sum(item["count"] for item in responses.values()),
            "response_sources": list(responses.values())}, active


class NotificationView:
    def __init__(self, store):
        self.store = store
        self.epoch = None
        self.boot = None
        self.version = self.attempt = 0
        self.mode = "home"
        self.page = self.pages = 1
        self.anchor = None
        self.rows = []
        self.items = {}
        self.signature = None
        self.selected = None
        self.detail_sent = self.detail_seen = False
        self.detail_crc = None
        self.pending_home_display = None

    def _reset(self, epoch, boot, device_page):
        # A new connection resets protocol versions. On the same boot the
        # board still has its open page and selected row.
        same_list = (boot is not None and boot == self.boot and device_page in (1, 2, 3)
                     and self.mode in ("list", "detail"))
        page, anchor = (self.page, self.anchor) if same_list else (1, None)
        selected = self.selected if same_list and device_page == 2 else None
        self.epoch = epoch
        self.boot = boot
        self.version = self.attempt = 0
        self.mode = "detail" if selected else "list" if device_page in (1, 2, 3) else "home"
        self.page, self.pages, self.anchor, self.rows, self.items = page, 1, anchor, [], {}
        self.signature = None
        self.selected = selected
        self.detail_sent = self.detail_seen = False
        self.detail_crc = None
        self.pending_home_display = None

    def touch(self, event, *, channel=None):
        action = event.get("action")
        version = event.get("version")
        cache_id = event.get("id")
        local_list = action == "list" and isinstance(cache_id, str) and bool(cache_id)
        epoch, active_version = self.epoch, self.version
        if local_list and channel is not None and channel.epoch != epoch:
            epoch, active_version = channel.epoch, 0
        minimum_version = 0 if local_list else 1
        if (type(version) is not int or not minimum_version <= version <= active_version or
                event.get("epoch", epoch) != epoch):
            return False
        if local_list and epoch != self.epoch:
            self._reset(epoch, channel.boot, 1)
        if action in ("home", "list"):
            # These destinations are independent of the rows on the screen.
            if event.get("id") != "" and action != "list":
                return False
        elif version != self.version:
            return False
        if action == "home":
            self.mode, self.anchor, self.page, self.selected = "home", None, 1, None
        elif action == "list":
            if not isinstance(cache_id, str):
                return False
            if cache_id:
                include_responses = channel is not None and channel.capabilities.get("agent_inbox_read") == 1
                anchor = self.store.mark_list_seen_through_cache_record(cache_id, include_responses=include_responses)
                if anchor is None:
                    return False
                self.page, self.anchor = 1, anchor
            elif self.mode == "home":
                self.page, self.anchor = 1, None
            self.mode, self.selected = "list", None
        elif action in ("previous", "next") and self.mode == "list":
            candidate = self.page + (1 if action == "next" else -1)
            if not 1 <= candidate <= self.pages:
                return False
            self.page = candidate
        elif action == "detail" and self.mode == "list" and event.get("id") in self.items:
            self.selected = self.items[event["id"]]
            self.mode = "detail"
            self.detail_sent = self.detail_seen = False
            self.detail_crc = None
        else:
            return False
        self.signature = None
        return True

    async def refresh(self, client, *, device_page=None, displayed_version=None, role_ready=True, cached_records=None):
        channel = client.channel
        if channel.capabilities.get("agent_home") != 1:
            return False
        if self.epoch != channel.epoch:
            self._reset(channel.epoch, channel.boot, device_page)
        if self.pending_home_display is not None and device_page == 0:
            version, identities = self.pending_home_display
            if displayed_version == version:
                self.store.mark_home_displayed(identities)
                self.pending_home_display = None
        cache_first = channel.capabilities.get("agent_cache_first") == 1
        if channel.capabilities.get("agent_local_inbox") == 1:
            if device_page in (1, 2, 3):
                return False
            if self.mode != "home":
                if device_page != 0:
                    return False
                # Menu navigation and lost touch receipts can skip the home event.
                self.mode, self.anchor, self.page, self.selected = "home", None, 1, None
        if cache_first and cached_records is None and (self.mode == "home" or self.anchor is None):
            return False
        if self.mode == "detail" and self.version:
            return await self._detail(client)
        attention = 0
        if cache_first:
            records = cached_records or ()
            inbox_read = channel.capabilities.get("agent_inbox_read") == 1
            cleared = self.store.seen_notifications(
                [item["id"] for item in records if not item["seen"] and (inbox_read or item["category"] == "completed")],
                include_resolved=inbox_read)
            summary, active = _cached_home_summary((item for item in records if item["id"] not in cleared),
                                                   unread_responses=inbox_read)
            receipts, attention = self.store.cached_home_receipts(active) if self.mode == "home" else ((), 0)
        elif self.mode == "home":
            summary, receipts = self.store.home_summary(include_receipts=True, for_device=True)
        else:
            summary, receipts = self.store.home_summary(for_device=True), ()
        waiting = min(65535, summary["response_count"])
        results = min(65535, summary["completed_count"])
        if self.mode == "home":
            signature = json.dumps(["home", summary], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if not (waiting and role_ready and device_page in (None, 0) and channel.capabilities.get("response_attention") == 1):
                attention = 0
            elif not cache_first:
                attention = self.store.pending_attention()
            queued = self.pending_home_display[1] if self.pending_home_display is not None else ()
            if self.signature == signature and not attention and receipts == queued:
                return True
            entries = home_lines(summary)
            rows = []
            for entry in entries:
                # The suffix describes the bitmap's icon slots. Older firmware
                # treats this ID as opaque and still draws the same bitmap.
                identity = "home-" + entry["category"]
                if entry["marks"]:
                    identity += "-" + entry["marks"]
                rows.append({"id": identity, "kind": 3 if entry["category"] == "completed" else 0,
                             "title": entry["bitmap"].hex()})
            extra = {"agent_home": True}
            if attention:
                extra["response_attention"] = f"{attention:016x}"
            items = {}
        else:
            anchor = self.anchor
            if cache_first and anchor is None:
                anchor = max((item["id"] for item in cached_records), default=0)
            page = self.store.list_page(self.page, page_size=4, anchor=anchor,
                                        pending_first=channel.capabilities.get("agent_inbox_read") == 1,
                                        clamp=True, for_device=True)
            self.page = page["page"]
            if cache_first and self.anchor is None:
                # The first live page can replace the TF page only if they
                # have the same identities and layout. Later live pages can
                # use the full retained history below this fixed anchor.
                cached_rows = [(item["id"], notification_row(item)) for item in cached_records[:4]]
                live_rows = [(item["id"], notification_row(item)) for item in page["items"]]
                if cached_rows != live_rows:
                    return False
            if page["pages"] > 65535:
                raise ValueError("notification_pages_exceed_device_limit")
            signature = json.dumps(["list", page, waiting, results], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if self.signature == signature:
                return True
            items = {f"n-{item['id']}": item for item in page["items"]}
            rows = [{"id": key, "revision": 1, **notification_row(item)} for key, item in items.items()]
            extra = {"pagination": [self.page - 1, page["pages"]]}
        responses = {source["id"] for source in summary["response_sources"]}
        responses.update(item["id"] for item in items.values() if item["category"] == "response" and not item["resolved"])
        value = {"count": len(rows), "waiting": waiting, "results": results, "running": 0, **extra}
        self.attempt += 1
        version = self.attempt
        async with client._operation:
            self.store.record_response_projection("screen", responses)
            await channel.send("snapshot_begin", version=version, **value)
            for index, row in enumerate(rows):
                await channel.send("row", version=version, index=index, **row)
            result = await channel.send("snapshot_commit", version=version)
            if result.get("committed_version") != version:
                raise RuntimeError("notification_snapshot_not_committed")
            self.store.record_response_projection("screen", responses, confirmed=True)
        self.rows, self.items, self.signature, self.version = rows, items, signature, version
        if self.mode != "home":
            self.anchor, self.pages = page["anchor"], page["pages"]
        if receipts:
            self.store.mark_home_shown(receipts)
        self.pending_home_display = (version, receipts) if receipts else None
        if attention:
            self.store.acknowledge_attention(attention)
        if self.mode == "detail":
            identity = f"n-{self.selected['id']}"
            self.selected = self.items.get(identity)
            if self.selected is not None:
                return await self._detail(client)
            self.mode = "list"
        return True

    async def _detail(self, client):
        item = self.selected
        identity = f"n-{item['id']}"
        channel = client.channel
        async with client._operation:
            if not self.detail_sent:
                pixels = _detail_pixels(item)
                checksum = zlib.crc32(pixels)
                fields = {"id": identity, "revision": 1, "page": 0}
                result = await channel.send("detail_begin", **fields, pages=1,
                                            short_id=f"{item['id'] & 0xffffffff:08x}",
                                            reported=datetime.fromtimestamp(item["created_at"]).strftime("%Y-%m-%d %H:%M"),
                                            fresh=False, crc=checksum)
                if result.get("detail_cancelled"):
                    return False
                for index in range(16):
                    result = await channel.send("detail_chunk", **fields, index=index,
                                                data=pixels[index * 252:(index + 1) * 252].hex())
                    if result.get("detail_cancelled"):
                        return False
                result = await channel.send("detail_commit", **fields)
                if result.get("detail_ready") is not True:
                    return False
                self.detail_sent = True
                self.detail_crc = checksum
            if not self.detail_seen:
                status = await channel.send("heartbeat")
                if (status.get("detail_displayed") is True and status.get("detail_crc") == self.detail_crc
                        and status.get("detail_revision") == 1 and status.get("detail_page") == 0):
                    self.store.mark_seen(item["id"])
                    self.detail_seen = True
        return True
