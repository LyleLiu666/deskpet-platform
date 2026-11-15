"""Persist recent notifications on devices that support individual offline reads."""
import base64
from datetime import datetime
import struct
import time
import zlib

from .channel import CommandRejected
from .notification_detail import render_cached_detail
from .notification_graphics import notification_row
from .notification_view import _detail_pixels


CACHE_RANGE = 8
DETAIL_CHUNK_BYTES = 252


def _fingerprint(item, bitmap):
    metadata = struct.pack("<BIIQQBB", item["kind"], item["revision"], item["content_revision"],
                           item["created_ms"], item["updated_ms"], item["unread"], 0)
    details = item["detail_crc"]
    canonical = (item["id"].encode("ascii") + b"\0" + metadata + base64.b64decode(item["title"]) + b"\0" +
                 base64.b64decode(bitmap[1:]) + struct.pack(f"<B{len(details)}I", len(details), *details))
    return f"{zlib.crc32(canonical):08x}"


def _prepare(record, *, finished_gray=False, feedback_dismissal=False):
    if feedback_dismissal and record["category"] == "response" and not record["resolved"]:
        session = record.get("session", "")[:8]
        display = {**record, "title": f'{session} · {record["title"]}'[:160]}
        row = notification_row(display)
        timestamp = int(record["created_at"] * 1000)
        revision = zlib.crc32((record["cache_id"] + ":feedback").encode("ascii")) or 1
        return (dict(id=record["cache_id"], kind=5, revision=1, content_revision=revision,
                     created_ms=timestamp, updated_ms=timestamp, unread=not record["seen"],
                     title=base64.b64encode(display["title"].encode()).decode("ascii"), detail_crc=[]),
                row["title"], None, None)
    if "session" in record:
        rendered = render_cached_detail(record, finished_gray=finished_gray)
        pixels = rendered.pop("pixels")
    else:
        rendered = {}
        pixels = _detail_pixels(record)
    crc = zlib.crc32(pixels)
    timestamp = int(record["created_at"] * 1000)
    reported = datetime.fromtimestamp(record["created_at"]).strftime("%Y-%m-%d %H:%M")
    short_id = rendered.get("short_id", f"{record['id'] & 0xffffffff:08x}")
    content_key = f"{reported}:{short_id}:{crc}" if rendered else reported
    content_revision = zlib.crc32(content_key.encode("ascii")) or 1
    row = notification_row(record)
    title = record["title"] or "消息"
    list_record = dict(id=record["cache_id"], kind=row["kind"], revision=1, content_revision=content_revision,
                       created_ms=timestamp, updated_ms=timestamp, unread=not record["seen"],
                       title=base64.b64encode(title.encode()).decode("ascii"), detail_crc=[crc])
    detail = dict(id=record["cache_id"], content_revision=content_revision, page=0, pages=1,
                  short_id=short_id,
                  reported=reported, fresh=False, crc=crc, gray=rendered.get("gray", False))
    return list_record, row["title"], detail, pixels


class NotificationCache:
    def __init__(self, store):
        self.store = store
        self.epoch = None
        self.transfer = None
        self.confirmed = None
        self.confirmed_epoch = None
        self.poll_at = 0
        self.retry_at = 0
        self.reuse_failed = False
        self.session_ids = False

    def committed_records(self, channel):
        if self.confirmed is not None and channel.epoch == self.confirmed_epoch and channel.boot == self.confirmed[0]:
            return self.confirmed[2]

    def _records(self, channel):
        return self.store.cache_records(pending_first=channel.capabilities.get("agent_inbox_read") == 1,
                                        active_only=channel.capabilities.get("agent_local_inbox") == 1,
                                        session_ids=self.session_ids)

    async def step(self, client):
        channel = client.channel
        if channel.capabilities.get("agent_cache_selective") != 1:
            return
        cache_first = channel.capabilities.get("agent_cache_first") == 1
        if channel.epoch != self.epoch:
            self.epoch = channel.epoch
            self.transfer = None
            self.poll_at = self.retry_at = 0
            self.reuse_failed = False
            self.session_ids = False
        if self.transfer is None:
            now = time.monotonic()
            if now < self.retry_at:
                return
            committed = self.committed_records(channel)
            if now < self.poll_at and (not cache_first or committed is None or committed == self._records(channel)):
                return
            self.transfer = self._sync(channel)
        async with client._operation:
            # Bound the amount of work between heartbeats. Use spare time on
            # fast links to make a complete card commit sooner.
            deadline = time.monotonic() + .25
            for index in range(32 if cache_first else 8):
                if cache_first and index and time.monotonic() >= deadline:
                    return
                if not channel.events.empty():
                    return
                try:
                    await anext(self.transfer)
                except StopAsyncIteration:
                    self.transfer = None
                    self.poll_at = time.monotonic() + 5
                    return
                except CommandRejected as error:
                    self.transfer = None
                    self.poll_at = self.retry_at = time.monotonic() + 5
                    if error.code in ("card_busy", "role_busy"):
                        return
                    await channel.send("cache_abort")
                    raise

    async def _sync(self, channel):
        state = await channel.send("cache_state")
        self.session_ids = state.get("cache_sessions") == 1
        if not state.get("cache_available"):
            self.confirmed_epoch = None
            self.confirmed = None
            return
        generation = state["cache_generation"]
        if self.confirmed is not None and (channel.boot, generation) == self.confirmed[:2]:
            self.confirmed_epoch = channel.epoch
        elif self.confirmed_epoch != channel.epoch:
            self.confirmed_epoch = None
        yield
        if state.get("cache_busy"):
            return
        records = self._records(channel)
        if (channel.boot, generation, records) == self.confirmed:
            return
        supports_dismissal = channel.capabilities.get("cache_feedback_dismissal") == 1
        receipt = await channel.send("cache_receipt_begin", **({"feedback_dismissal": True} if supports_dismissal else {}))
        yield
        if receipt.get("cache_busy"):
            return
        had_receipts = False
        while True:
            receipt = await channel.send("cache_receipt_next", batch=True)
            yield
            retry = receipt.get("cache_receipt_retry")
            if retry:
                if retry != "scan":
                    await channel.send("cache_abort")
                    return
                continue
            if not receipt["has_receipt"]:
                break
            had_receipts = True
            if receipt.get("feedback_dismissal") is True:
                if not supports_dismissal or not self.store.dismiss_cached_response(receipt["id"]):
                    await channel.send("cache_abort")
                    return
            else:
                self.store.mark_cached_seen(receipt["id"],
                                            grouped_completions=channel.capabilities.get("agent_local_inbox") == 1)
            await channel.send("cache_receipt_ack", id=receipt["id"],
                               content_revision=receipt["content_revision"])
            yield
        await channel.send("cache_receipt_finish")
        yield
        records = self._records(channel)
        active = {}
        reuse = channel.capabilities.get("agent_cache_reuse") == 1 and not self.reuse_failed
        count = state.get("cache_count")
        if reuse:
            if type(count) is not int or not 0 <= count <= 1000:
                raise RuntimeError("invalid_cache_count")
            for start in range(0, count, CACHE_RANGE):
                size = min(CACHE_RANGE, count - start)
                manifest = await channel.send("cache_manifest", generation=generation, start=start, count=size)
                yield
                if manifest.get("cache_changed"):
                    await channel.send("cache_abort")
                    self.confirmed_epoch = None
                    return
                rows = manifest.get("rows")
                if not isinstance(rows, list) or len(rows) != size:
                    await channel.send("cache_abort")
                    raise RuntimeError("invalid_cache_manifest")
                for index, fingerprint in enumerate(rows, start):
                    if fingerprint is None:
                        continue
                    if (not isinstance(fingerprint, str) or len(fingerprint) != 8 or
                            any(char not in "0123456789abcdef" for char in fingerprint)):
                        await channel.send("cache_abort")
                        raise RuntimeError("invalid_cache_manifest")
                    active[fingerprint] = index if fingerprint not in active else None
        prepared = []
        finished_gray = channel.capabilities.get("agent_local_inbox") == 1
        for record in records:
            item, bitmap, detail, pixels = _prepare(record, finished_gray=finished_gray,
                                                    feedback_dismissal=supports_dismissal)
            reusable = active.get(_fingerprint(item, bitmap)) if reuse else None
            prepared.append((item, bitmap, reusable))
            if reusable is not None or detail is None:
                continue
            reply = await channel.send("cache_detail_begin", **detail)
            yield
            if not reply.get("cached"):
                start = reply.get("next_chunk", 0)
                chunks = len(pixels) // DETAIL_CHUNK_BYTES
                if type(start) is not int or not 0 <= start <= chunks:
                    raise RuntimeError("invalid_cache_resume")
                # A resume may start on an odd chunk, so its final chunk may be unpaired.
                for chunk in range(start, chunks, 2):
                    block = pixels[chunk * DETAIL_CHUNK_BYTES:(chunk + 2) * DETAIL_CHUNK_BYTES]
                    data = "~" + base64.b64encode(block).decode("ascii") if len(block) == 2 * DETAIL_CHUNK_BYTES else block.hex()
                    await channel.send("cache_detail_chunk", index=chunk, data=data)
                    yield
                await channel.send("cache_detail_commit")
                yield
        same_manifest = (reuse and not had_receipts and generation and len(prepared) == count and
                         all(source == index for index, (_, _, source) in enumerate(prepared)))
        if same_manifest:
            self.confirmed = (channel.boot, generation, records)
            self.confirmed_epoch = channel.epoch
            return
        reply = await channel.send("cache_begin", generation=generation + 1, count=len(prepared))
        yield
        if reply.get("cache_busy") or reply.get("cache_changed"):
            if reply.get("cache_changed"):
                # Another card writer changed the generation before upload.
                # Keep the earlier commit only when the next probe recognizes it.
                self.confirmed_epoch = None
            return
        index = 0
        while index < len(prepared):
            item, bitmap, source = prepared[index]
            if source is not None:
                size = 1
                while (size < CACHE_RANGE and index + size < len(prepared) and
                       prepared[index + size][2] == source + size):
                    size += 1
                reply = await channel.send("cache_copy", generation=generation, start=source, count=size)
                yield
                if reply.get("cache_changed"):
                    await channel.send("cache_abort")
                    self.confirmed_epoch = None
                    return
                if reply.get("cache_reuse_failed"):
                    await channel.send("cache_abort")
                    self.reuse_failed = True
                    return
                index += size
                continue
            await channel.send("cache_item", **item)
            yield
            await channel.send("cache_bitmap", bitmap=bitmap)
            yield
            await channel.send("cache_item_commit")
            yield
            index += 1
        reply = await channel.send("cache_commit")
        if reply.get("cache_generation") != generation + 1:
            raise RuntimeError("notification_cache_not_committed")
        self.confirmed = (channel.boot, generation + 1, records)
        self.confirmed_epoch = channel.epoch
        self.reuse_failed = False
        yield
