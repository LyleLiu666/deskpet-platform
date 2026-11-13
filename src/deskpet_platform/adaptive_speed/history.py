"""Numeric-only second samples and exact bounded-window distributions."""
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
import time
from .config import Config

RECENT_SECONDS=1800
LONG_SECONDS=7*86400


@dataclass(frozen=True)
class Frame:
    sample_id: str
    captured_at: float
    rate10: int
    status: str
    metric_version: str
    demo: bool = False


def frame_error(frame, received_at):
    valid_text=lambda s,n:isinstance(s,str) and 0<len(s)<=n and all(ord(c)>=32 for c in s)
    if not valid_text(frame.sample_id,128) or not valid_text(frame.metric_version,128) or frame.metric_version=='unknown' or type(frame.rate10) is not int or not 0<=frame.rate10<=10000000 or type(frame.captured_at) not in (int,float) or not math.isfinite(frame.captured_at) or frame.status not in ('L','E','I','W','N') or type(frame.demo) is not bool:
        return 'invalid'
    if frame.demo:return 'demo'
    if frame.captured_at>received_at+1:return 'future'
    if received_at-frame.captured_at>3:return 'stale'
    return None


def _at(values, p):
    if not values:return None
    at=(len(values)-1)*p;lo=int(at);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(at-lo)


def quantile(values,p):
    if not 0<=p<=1:raise ValueError('invalid_quantile')
    return _at(sorted(values),p)


@dataclass(frozen=True)
class Distribution:
    count: int
    minimum: float | None
    p10: float | None
    p20: float | None
    p25: float | None
    p40: float | None
    median: float | None
    p70: float | None
    p80: float | None
    p90: float | None
    maximum: float | None

    @classmethod
    def of(cls, values):
        v=sorted(values)
        return cls(len(v),v[0] if v else None,*(_at(v,p) for p in (.1,.2,.25,.4,.5,.7,.8,.9)),v[-1] if v else None)


@dataclass(frozen=True)
class Window:
    recent: Distribution
    long: Distribution
    periods: int
    diagnostics: dict[str,int]
    recent_ready: bool
    long_ready: bool


def _window(db, version, now, config=Config()):
    now=math.floor(now)
    boundary=now-config.recent_seconds
    rows=db.execute('SELECT stamp,rate10,kind,status FROM speed_sample WHERE metric_version=? AND stamp>=? AND stamp<? ORDER BY stamp',
                    (version,boundary-config.long_days*86400,now)).fetchall()
    recent=[r for r in rows if r[0]>=boundary];older=[r for r in rows if r[0]<boundary and r[2]=='positive']
    positive=[r[1]/10 for r in recent if r[2]=='positive']
    periods=sum(i==0 or r[0]-older[i-1][0]>=1800 for i,r in enumerate(older))
    diagnostics={kind+'_seconds':sum(r[2]==kind for r in recent) for kind in ('positive','zero','missing','idle')}
    first=db.execute('SELECT MIN(stamp) FROM speed_sample WHERE metric_version=?',(version,)).fetchone()[0]
    expected=max(0,now-max(boundary,first)) if first is not None else 0
    diagnostics['reported_missing_seconds']=diagnostics['missing_seconds']
    diagnostics['unobserved_seconds']=max(0,expected-len(recent))
    diagnostics['missing_seconds']+=diagnostics['unobserved_seconds']
    diagnostics['valid_seconds']=sum(r[2]!='missing' for r in recent)
    return Window(Distribution.of(positive),Distribution.of([r[1]/10 for r in older]),periods,diagnostics,len(positive)>=config.recent_minimum,len(older)>=7200 and periods>=2)


class History:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.recovered=False;self.db=None;self.last_prune=None
        try:
            try:
                self.db=sqlite3.connect(self.path,timeout=.25)
                if self.db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    error=sqlite3.DatabaseError('invalid_speed_history')
                    error.sqlite_errorcode=sqlite3.SQLITE_CORRUPT
                    raise error
            except sqlite3.DatabaseError as error:
                # Busy, inaccessible or full storage is not evidence of corruption.
                if getattr(error,'sqlite_errorcode',0)&255 not in (sqlite3.SQLITE_CORRUPT,sqlite3.SQLITE_NOTADB):raise
                if self.db:self.db.close();self.db=None
                suffix='.corrupt-'+str(time.time_ns())
                for extra in ('','-wal','-shm'):
                    source=Path(str(self.path)+extra)
                    if source.exists():source.rename(Path(str(self.path)+suffix+extra))
                self.db=sqlite3.connect(self.path,timeout=.25);self.recovered=True
            self.path.chmod(0o600)
            self.db.execute('PRAGMA journal_mode=WAL')
            with self.db:
                self.db.execute('CREATE TABLE IF NOT EXISTS speed_sample (metric_version TEXT NOT NULL,stamp INTEGER NOT NULL,sample_id TEXT NOT NULL,rate10 INTEGER NOT NULL,status TEXT NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(metric_version,stamp),UNIQUE(metric_version,sample_id))')
                self.db.execute('CREATE TABLE IF NOT EXISTS speed_diagnostic (metric_version TEXT NOT NULL,reason TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(metric_version,reason))')
        except (sqlite3.Error,OSError):
            if self.db:self.db.close();self.db=None
            raise

    def close(self):self.db.close()

    def add(self, frame: Frame, received_at):
        valid_text=lambda s,n:isinstance(s,str) and 0<len(s)<=n and all(ord(c)>=32 for c in s)
        version=frame.metric_version if valid_text(frame.metric_version,128) else 'unknown'
        reason=None
        reason=frame_error(frame,received_at)
        if not reason and self.db.execute('SELECT 1 FROM speed_sample WHERE metric_version=? AND sample_id=?',(version,frame.sample_id)).fetchone():reason='duplicate'
        elif not reason and self.db.execute('SELECT 1 FROM speed_sample WHERE metric_version=? AND stamp=?',(version,math.floor(frame.captured_at))).fetchone():reason='same_second'
        with self.db:
            if reason:
                self.db.execute('INSERT INTO speed_diagnostic VALUES(?,?,1) ON CONFLICT(metric_version,reason) DO UPDATE SET count=count+1',(version,reason))
                return reason
            kind='missing' if frame.status in ('W','N') else 'zero' if frame.rate10==0 else 'idle' if frame.status=='I' else 'positive'
            self.db.execute('INSERT INTO speed_sample VALUES(?,?,?,?,?,?)',(version,math.floor(frame.captured_at),frame.sample_id,frame.rate10,frame.status,kind))
            if self.last_prune is None or received_at-self.last_prune>=3600:
                self.db.execute('DELETE FROM speed_sample WHERE stamp<?',(received_at-3600-LONG_SECONDS,))
                self.last_prune=received_at
        return kind

    def window(self,version,now,config=Config()):return _window(self.db,version,now,config)

    def recent_rates(self,version,end):
        rows=self.db.execute(
            'SELECT stamp,rate10,kind FROM speed_sample WHERE metric_version=? AND stamp BETWEEN ? AND ?',
            (version,end-59,end),
        )
        return {stamp: None if kind=='missing' else rate10 for stamp,rate10,kind in rows}

    @staticmethod
    def read_window(path,version,now,config=Config()):
        # The statistics worker owns this read connection; the service owns writes.
        db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.25)
        try:
            db.execute('BEGIN')
            return _window(db,version,now,config)
        finally:db.close()
