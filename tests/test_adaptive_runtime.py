import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from deskpet_platform.adaptive_speed.history import History,Frame,Distribution,Window
from deskpet_platform.adaptive_speed.runtime import AdaptiveRuntime


def snapshot(median=90):
    return Window(Distribution.of([median]*300),Distribution.of(list(range(10,110))*80),2,{},True,True)


class AdaptiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();self.now=1_800_000_000.
        self.history=History(Path(self.folder.name)/'history.sqlite3');self.calls=[];self.value=snapshot()
        def reader(path,version,now,config=None):self.calls.append((version,now));return self.value
        self.runtime=AdaptiveRuntime(self.history,clock=lambda:self.now,monotonic=lambda:self.now,reader=reader)
    async def asyncTearDown(self):await self.runtime.close();self.history.close();self.folder.cleanup()
    def observe(self,version='v1',rate10=900):
        self.runtime.observe(Frame(str(self.now),self.now,rate10,'L',version),self.now)
    async def flush(self):
        await self.runtime.tick();await asyncio.sleep(.02);await self.runtime.tick()

    async def test_recent_refreshes_by_minute_and_baseline_stays_fixed_for_hour(self):
        self.observe();await self.flush()
        self.assertEqual(len(self.calls),1)
        self.value=Window(snapshot().recent,Distribution.of([500]*8000),2,{},True,True)
        self.now+=59;self.observe();await self.flush();self.assertEqual(len(self.calls),1)
        self.now+=1;self.observe();await self.flush();self.assertEqual(len(self.calls),2)
        self.assertEqual(self.runtime.window.long.median,59.5)
        self.now+=3540;self.observe();await self.flush()
        self.assertEqual(self.runtime.window.long.median,500)

    async def test_read_worker_does_not_block_loop_and_old_version_result_is_ignored(self):
        started=threading.Event();release=threading.Event()
        def slow(path,version,now,config=None):
            if version=='v1':started.set();release.wait(2)
            return snapshot(90 if version=='v1' else 20)
        self.runtime.reader=slow
        self.observe();await self.runtime.tick()
        for _ in range(100):
            if started.is_set():break
            await asyncio.sleep(.001)
        self.assertTrue(started.is_set())
        self.now+=1;self.observe('v2');await self.runtime.tick()
        self.assertEqual(self.runtime.version,'v2')
        release.set();await self.flush();await self.flush()
        self.assertEqual(self.runtime.window.recent.median,20)

    async def test_baseline_restores_by_version_and_stale_source_hides_energy(self):
        self.observe();await self.flush()
        await self.runtime.close()
        self.runtime=AdaptiveRuntime(self.history,clock=lambda:self.now,monotonic=lambda:self.now)
        self.observe()
        self.assertEqual(self.runtime.reference.median,59.5)
        self.now+=4;await self.runtime.tick()
        self.assertEqual(self.runtime.state.current,0)
        self.assertEqual(self.runtime.state.overall,0)
        self.assertEqual(self.runtime.state.reason,'missing')
        self.observe('v2');self.assertIsNone(self.runtime.reference)

    async def test_wire_is_versioned_and_never_refreshes_expired_sample(self):
        self.observe();await self.flush();self.now+=1;self.observe();await self.flush()
        wire=self.runtime.wire()
        self.assertEqual(wire['version'],1);self.assertEqual(wire['current'],3)
        self.assertEqual(wire['age_ms'],0)
        self.now+=4
        self.assertIsNone(self.runtime.wire())
        self.runtime.observe(None,self.now)
        self.assertIsNone(self.runtime.wire())

    async def test_wire_classifies_new_reading_before_next_background_tick(self):
        self.observe();await self.flush();self.now+=1;self.observe();await self.flush()
        self.assertGreater(self.runtime.wire()['current'],0)
        self.now+=1;self.observe(rate10=0)
        self.assertEqual(self.runtime.wire()['current'],0)

    async def test_minute_judgment_does_not_count_same_cached_distribution_twice(self):
        self.observe();await self.flush()
        self.now+=61;self.observe()
        # The next asynchronous read has not completed. The old snapshot gets no vote.
        await self.runtime.tick()
        self.assertEqual(self.runtime.state.overall,0)
        await self.flush()
        self.assertEqual(self.runtime.state.overall,3)

    async def test_changed_metric_clears_previous_wire_calibration_immediately(self):
        self.observe();await self.flush();self.now+=61;self.observe();await self.flush()
        self.assertEqual(self.runtime.state.overall,3)
        self.observe('v2')
        self.assertFalse(self.runtime.wire()['calibrated'])
        self.assertEqual(self.runtime.wire()['overall'],0)


    async def test_switch_rebuilds_distribution_without_deleting_history_and_restores_matching_reference(self):
        self.observe();await self.flush()
        reference=self.runtime.reference.median
        old_key=self.runtime.reference_key
        self.runtime.configure(dict(recent_seconds=120,long_days=3))
        self.assertEqual(self.runtime.config.recent_minimum,20)
        self.assertEqual(self.runtime.window.recent.count,0)
        self.assertFalse(self.runtime.wire()['calibrated'])
        self.assertNotEqual(self.runtime.reference_key,old_key)
        await self.flush()
        self.assertEqual(self.runtime.status()['windows']['recent_seconds'],120)
        self.assertEqual(self.history.db.execute('SELECT COUNT(*) FROM speed_reference').fetchone()[0],2)
        self.runtime.configure(dict(recent_seconds=1800,long_days=7))
        self.assertEqual(self.runtime.reference.median,reference)



    async def test_clock_rollback_rebuilds_cache_and_ignores_pre_jump_worker(self):
        mono=[100.];self.runtime.monotonic=lambda:mono[0]
        self.observe();await self.flush()
        self.now-=300;mono[0]+=1;self.observe();await self.flush()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.runtime.window_at,self.now)
        self.assertEqual(self.runtime.reference_at,self.now)
        self.assertTrue(self.runtime.state.calibrated)
        mono[0]+=60;self.now+=60;self.observe();await self.flush()
        self.assertEqual(len(self.calls),3)

    async def test_wall_clock_step_during_read_does_not_accept_old_snapshot(self):
        mono=[100.];self.runtime.monotonic=lambda:mono[0]
        started=threading.Event();release=threading.Event()
        def slow(path,version,now,config):
            self.calls.append(now)
            if len(self.calls)==1:started.set();release.wait(2)
            return snapshot(90 if len(self.calls)==1 else 20)
        self.runtime.reader=slow;self.observe();await self.runtime.tick()
        try:
            for _ in range(100):
                if started.is_set():break
                await asyncio.sleep(.001)
            self.assertTrue(started.is_set())
            self.now-=300;mono[0]+=1;self.observe();await self.runtime.tick()
        finally:release.set()
        await self.flush();await self.flush()
        self.assertEqual(self.runtime.window_at,self.now)
        self.assertEqual(self.runtime.window.recent.median,20)

    async def test_refresh_interval_uses_monotonic_time_despite_small_wall_adjustment(self):
        mono=[100.];self.runtime.monotonic=lambda:mono[0]
        self.observe();await self.flush()
        self.now+=59.5;mono[0]+=60;self.observe();await self.flush()
        self.assertEqual(len(self.calls),2)


    async def test_forward_clock_step_replaces_stale_history_instead_of_showing_old_level(self):
        mono=[100.];self.runtime.monotonic=lambda:mono[0]
        self.observe();await self.flush()
        self.now+=7200;mono[0]+=1;self.value=Window(snapshot().recent,Distribution.of([500]*8000),2,{},True,True)
        self.observe();await self.flush()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.runtime.reference_at,self.now)
        self.assertEqual(self.runtime.reference.median,500)


    async def test_bad_persisted_reference_timestamp_rewarms_and_repairs_the_cache(self):
        import json
        from dataclasses import asdict
        self.observe();await self.flush()
        key=json.dumps(['v1',120,3])
        for timestamp in ('invalid-timestamp',float('inf')):
            with self.subTest(timestamp=timestamp):
                self.runtime.configure(dict(recent_seconds=1800,long_days=7))
                with self.history.db:self.history.db.execute('INSERT OR REPLACE INTO speed_reference VALUES(?,?,?,?,?)',
                    (key,timestamp,json.dumps(asdict(snapshot().long)),2,1))
                self.runtime.configure(dict(recent_seconds=120,long_days=3))
                self.assertEqual(self.runtime.last_error,'reference_rebuilt')
                self.assertIsNone(self.runtime.reference)
                self.assertFalse(self.runtime.wire()['calibrated'])
                await self.flush()
                self.assertEqual(self.history.db.execute('SELECT computed_at FROM speed_reference WHERE metric_version=?',(key,)).fetchone()[0],self.now)
