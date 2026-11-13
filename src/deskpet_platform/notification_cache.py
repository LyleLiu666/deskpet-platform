"""Persist recent notifications on devices that support individual offline reads."""
import base64
from datetime import datetime
import time
import zlib

from .channel import CommandRejected
from .notification_graphics import notification_row
from .notification_view import _detail_pixels


def _prepare(record):
    pixels = _detail_pixels(record)
    crc = zlib.crc32(pixels)
    timestamp = int(record["created_at"] * 1000)
    reported = datetime.fromtimestamp(record["created_at"]).strftime("%Y-%m-%d %H:%M")
    content_revision = zlib.crc32(reported.encode("ascii")) or 1
    row = notification_row(record)
    title = record["title"] or "消息"
    list_record = dict(id=record["cache_id"], kind=row["kind"], revision=1, content_revision=content_revision,
                       created_ms=timestamp, updated_ms=timestamp, unread=not record["seen"],
                       title=base64.b64encode(title.encode()).decode("ascii"), detail_crc=[crc])
    detail = dict(id=record["cache_id"], content_revision=content_revision, page=0, pages=1,
                  short_id=f"{record['id'] & 0xffffffff:08x}",
                  reported=reported, fresh=False, crc=crc, gray=False)
    return list_record, row["title"], detail, pixels


class NotificationCache:
    def __init__(self, store):
        self.store = store
        self.epoch = None
        self.transfer = None
        self.confirmed = None
        self.poll_at = 0

    async def step(self, client):
        channel = client.channel
        if channel.capabilities.get("agent_cache_selective") != 1:
            return
        if channel.epoch != self.epoch:
            self.epoch = channel.epoch
            self.transfer = None
            self.poll_at = 0
        if self.transfer is None:
            if time.monotonic() < self.poll_at:
                return
            self.transfer = self._sync(channel)
        async with client._operation:
            for _ in range(8):
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
                    self.poll_at = time.monotonic() + 5
                    self.confirmed = None
                    if error.code in ("card_busy", "role_busy"):
                        return
                    await channel.send("cache_abort")
                    raise

    async def _sync(self, channel):
        state = await channel.send("cache_state")
        yield
        if not state.get("cache_available") or state.get("cache_busy"):
            return
        generation = state["cache_generation"]
        records = self.store.cache_records()
        if (channel.boot, generation, records) == self.confirmed:
            return
        receipt = await channel.send("cache_receipt_begin")
        yield
        if receipt.get("cache_busy"):
            return
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
            self.store.mark_cached_seen(receipt["id"])
            await channel.send("cache_receipt_ack", id=receipt["id"],
                               content_revision=receipt["content_revision"])
            yield
        await channel.send("cache_receipt_finish")
        yield
        records = self.store.cache_records()
        prepared = []
        for record in records:
            item, bitmap, detail, pixels = _prepare(record)
            reply = await channel.send("cache_detail_begin", **detail)
            yield
            if not reply.get("cached"):
                start = reply.get("next_chunk", 0)
                if type(start) is not int or not 0 <= start <= 16:
                    raise RuntimeError("invalid_cache_resume")
                # A resume may start on an odd chunk, so its final chunk may be unpaired.
                for chunk in range(start, 16, 2):
                    block = pixels[chunk * 252:(chunk + 2) * 252]
                    data = "~" + base64.b64encode(block).decode("ascii") if len(block) == 504 else block.hex()
                    await channel.send("cache_detail_chunk", index=chunk, data=data)
                    yield
                await channel.send("cache_detail_commit")
                yield
            prepared.append((item, bitmap))
        reply = await channel.send("cache_begin", generation=generation + 1, count=len(prepared))
        yield
        if reply.get("cache_busy") or reply.get("cache_changed"):
            return
        for item, bitmap in prepared:
            await channel.send("cache_item", **item)
            yield
            await channel.send("cache_bitmap", bitmap=bitmap)
            yield
            await channel.send("cache_item_commit")
            yield
        reply = await channel.send("cache_commit")
        if reply.get("cache_generation") != generation + 1:
            raise RuntimeError("notification_cache_not_committed")
        self.confirmed = (channel.boot, generation + 1, records)
        yield
