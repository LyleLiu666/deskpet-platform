"""Async statistics: wall time selects history; monotonic time schedules its refresh."""
import asyncio
from dataclasses import asdict
import json
import math
import sqlite3
import time
from .history import Distribution,Window,History,Frame
from .classifier import Classifier
from .config import Config


TICK_SECONDS=.1


def empty_window():
    empty=Distribution.of([])
    return Window(empty,empty,0,{},False,False)


class AdaptiveRuntime:
    def __init__(self, history, clock=time.time, monotonic=time.monotonic, reader=History.read_window):
        self.config=Config();self.generation=0;self.job_generation=None
        self.history=history;self.clock=clock;self.monotonic=monotonic;self.reader=reader
        self.version=None;self.latest=None;self.window=empty_window();self.classifier=Classifier()
        self.reference=None;self.reference_at=None;self.reference_tick=None;self.reference_periods=0;self.reference_ready=False
        self.window_tick=None;self.job_tick=None;self.last_wall=None;self.last_tick=None
        self.window_at=None;self.job=None;self.job_at=None;self.worker=None;self.closed=False
        self.last_error=None;self.retry_at=None
        self.state=self.classifier.update(None,self.monotonic(),self.window)
        with history.db:history.db.execute('''CREATE TABLE IF NOT EXISTS speed_reference (
            metric_version TEXT PRIMARY KEY,computed_at REAL NOT NULL,distribution TEXT NOT NULL,
            periods INTEGER NOT NULL,ready INTEGER NOT NULL)''')

    @property
    def reference_key(self):return json.dumps([self.version,self.config.recent_seconds,self.config.long_days])

    def reset_statistics(self, restore_reference=True):
        self.generation+=1;self.window=empty_window();self.window_at=self.window_tick=None;self.retry_at=None
        self.reference=None;self.reference_at=self.reference_tick=None;self.reference_periods=0;self.reference_ready=False
        self.classifier=Classifier();self.state=self.classifier.update(None,self.monotonic(),self.window)
        if restore_reference:self.load_reference()

    def configure(self, value):
        config=Config.from_setting(value)
        if self.config==config:return
        self.config=config;self.reset_statistics()

    def load_reference(self):
        self.reference=None;self.reference_at=self.reference_tick=None;self.reference_ready=False;self.reference_periods=0
        self.last_error=None
        try:
            row=self.history.db.execute('SELECT computed_at,distribution,periods,ready FROM speed_reference WHERE metric_version=?',(self.reference_key,)).fetchone()
            if not row:return
            if type(row[0]) not in (int,float) or not math.isfinite(row[0]):raise ValueError('invalid reference timestamp')
            age=self.clock()-row[0]
            if not 0<=age<3600:return
            d=Distribution(**json.loads(row[1]));points=[v for k,v in asdict(d).items() if k!='count']
            if type(d.count) is not int or not 0<=d.count<=7*86400:raise ValueError('invalid reference count')
            if d.count:
                if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=1000000 for v in points):raise ValueError('invalid reference value')
                if points!=sorted(points):raise ValueError('invalid reference order')
            elif any(v is not None for v in points):raise ValueError('invalid empty reference')
            if type(row[2]) is not int or row[2]<0 or row[3] not in (0,1):raise ValueError('invalid reference period')
            if row[3] and (d.count<7200 or row[2]<2):raise ValueError('invalid warm reference')
            self.reference=d;self.reference_at=row[0];self.reference_tick=self.monotonic()-age;self.reference_periods=row[2];self.reference_ready=bool(row[3])
        except (ValueError,TypeError):self.last_error='reference_rebuilt'
        except (sqlite3.Error,OSError):self.last_error='reference_unavailable'

    def observe(self, frame: Frame | None, received_at):
        self.latest=frame
        if frame is None:return
        if self.version!=frame.metric_version:
            self.version=frame.metric_version;self.reset_statistics()

    def start(self):self.worker=asyncio.create_task(self.run())

    async def run(self):
        while not self.closed:
            await self.tick();await asyncio.sleep(TICK_SECONDS)

    async def close(self):
        self.closed=True
        if self.worker:await self.worker;self.worker=None
        if self.job:
            await asyncio.gather(self.job,return_exceptions=True);self.job=None

    def accept(self, snapshot):
        if self.reference_tick is None or self.job_tick-self.reference_tick>=3600:
            self.reference=snapshot.long;self.reference_at=self.job_at;self.reference_tick=self.job_tick
            self.reference_periods=snapshot.periods;self.reference_ready=snapshot.long_ready
            with self.history.db:self.history.db.execute('INSERT OR REPLACE INTO speed_reference VALUES(?,?,?,?,?)',
                (self.reference_key,self.reference_at,json.dumps(asdict(self.reference)),self.reference_periods,int(self.reference_ready)))
        self.window=Window(snapshot.recent,self.reference,self.reference_periods,snapshot.diagnostics,snapshot.recent_ready,self.reference_ready)
        self.window_at=self.job_at;self.window_tick=self.job_tick;self.last_error=None

    async def tick(self):
        now=self.clock();tick=self.monotonic()
        if self.last_wall is not None:
            wall_delta=now-self.last_wall;tick_delta=tick-self.last_tick
            if wall_delta<0 or tick_delta<0 or abs(wall_delta-tick_delta)>1:
                self.reset_statistics(restore_reference=False)
        self.last_wall=now;self.last_tick=tick
        if self.job and self.job.done():
            current=self.job_generation==self.generation
            try:
                result=self.job.result()
                if current:self.accept(result)
            except (sqlite3.Error,OSError,ValueError,TypeError):
                if current:
                    self.window=empty_window();self.last_error='statistics_unavailable';self.retry_at=tick+60
            self.job=None
        if self.latest and not self.job and (self.retry_at is None or tick>=self.retry_at) and (self.window_tick is None or tick-self.window_tick>=60):
            self.job_at=now;self.job_tick=tick;self.job_generation=self.generation
            self.job=asyncio.create_task(asyncio.to_thread(self.reader,self.history.path,self.version,now,self.config))
        self.update_state(now,tick)

    def update_state(self,now,tick):
        fresh=self.latest and not self.latest.demo and self.latest.status in ('L','E','I') and -1<=now-self.latest.captured_at<=3
        speed=(0 if self.latest.status=='I' else self.latest.rate10/10) if fresh else None
        snapshot=self.window if self.window_tick is not None and 0<=tick-self.window_tick<=120 else empty_window()
        self.state=self.classifier.update(speed,tick,snapshot,judgment_at=self.window_tick)

    def wire(self):
        now=self.clock()
        age=now-self.latest.captured_at if self.latest else None
        if age is None or not -1<=age<=3 or self.latest.demo or self.latest.status not in ('L','E','I'):return None
        self.update_state(now,self.monotonic())
        state=self.state
        return dict(version=1,overall=state.overall,current=state.current,calibrated=state.calibrated,
                    current_calibrated=state.current_calibrated,age_ms=max(0,int(age*1000)))

    def status(self):
        return dict(windows=dict(recent_seconds=self.config.recent_seconds,long_days=self.config.long_days,recent_minimum=self.config.recent_minimum),metric_version=self.version,reason=self.last_error or self.state.reason,overall=self.state.overall,
                    current=self.state.current,calibrated=self.state.calibrated,current_calibrated=self.state.current_calibrated,
                    recent_count=self.window.recent.count,long_count=self.window.long.count,reference_at=self.reference_at,
                    reading=self.job is not None,diagnostics=self.window.diagnostics)
