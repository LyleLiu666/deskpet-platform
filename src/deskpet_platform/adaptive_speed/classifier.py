"""Independent slow environment level and responsive five-tier motion."""
from bisect import bisect_right
from dataclasses import dataclass
import math
from .history import Window


@dataclass(frozen=True)
class SpeedState:
    overall: int
    current: int
    calibrated: bool
    current_calibrated: bool
    reason: str
    smoothed: float
    boundaries: tuple[float,...]


class Classifier:
    def __init__(self):
        self.environment=0;self.environment_valid=False;self.environment_candidate=None;self.environment_count=0;self.environment_at=None
        self.current=0;self.current_candidate=None;self.candidate_at=0
        self.smoothed=None;self.last_at=None

    @staticmethod
    def narrow(distribution):
        return distribution.count>0 and distribution.p90-distribution.p10<max(2,distribution.median*.1)

    @classmethod
    def baseline_ok(cls, distribution):
        return distribution.count>0 and not cls.narrow(distribution) and distribution.p25<distribution.median<distribution.p80

    def overall(self, now, window):
        if not window.recent_ready or not window.long_ready or not self.baseline_ok(window.long):
            self.environment=0;self.environment_valid=False;self.environment_candidate=None;self.environment_count=0;self.environment_at=None
            return
        if self.environment_at is not None and now-self.environment_at<60:return
        self.environment_at=now
        bounds=(window.long.p25,window.long.median,window.long.p80)
        median=window.recent.median;margin=window.long.median*.05
        target=bisect_right(bounds,median)
        if self.environment_valid:
            target=self.environment
            while target<3 and median>=bounds[target]+margin:target+=1
            while target>0 and median<bounds[target-1]-margin:target-=1
        if self.environment_valid and target==self.environment:
            self.environment_candidate=None;self.environment_count=0;return
        if target==self.environment_candidate:self.environment_count+=1
        else:self.environment_candidate=target;self.environment_count=1
        if self.environment_count>=2:
            self.environment=target;self.environment_valid=True;self.environment_candidate=None;self.environment_count=0

    def update(self, speed, now, window: Window, *, judgment_at=None):
        self.overall(now if judgment_at is None else judgment_at,window)
        calibrated=window.recent_ready and window.long_ready and self.baseline_ok(window.long)
        reason='warming_recent' if not window.recent_ready else 'warming_long' if not window.long_ready else 'narrow_baseline' if self.narrow(window.long) else 'tied_baseline' if not self.baseline_ok(window.long) else 'calibrated'
        bounds=(8.,25.,55.,85.)
        if window.recent_ready:
            d=window.recent
            if self.narrow(d) or not d.p20<d.p40<d.p70<d.p90:
                delta=max(2,d.median*.1)
                bounds=(d.median-2*delta,d.median-delta,d.median+delta,d.median+2*delta)
            else:bounds=(d.p20,d.p40,d.p70,d.p90)
        if type(speed) not in (int,float) or not math.isfinite(speed) or speed<0:
            self.current=0;self.current_candidate=None;self.smoothed=None;self.last_at=now
            return SpeedState(0,0,False,False,'missing',0,bounds)
        if speed==0:
            self.current=0;self.current_candidate=None;self.smoothed=0;self.last_at=now
            return SpeedState(0,0,calibrated,window.recent_ready,'zero',0,bounds)
        dt=max(0,now-self.last_at) if self.last_at is not None else 0
        if self.smoothed is None:self.smoothed=speed
        else:self.smoothed+=(speed-self.smoothed)*(1-math.exp(-dt/3))
        self.last_at=now
        target=bisect_right(bounds,self.smoothed)+1
        if target==self.current:self.current_candidate=None
        else:
            if self.current_candidate!=target:self.current_candidate=target;self.candidate_at=now
            duration=1 if target>self.current else 3
            if now-self.candidate_at>=duration:
                self.current=target;self.current_candidate=None
        return SpeedState(self.environment if calibrated else 0,self.current,calibrated,window.recent_ready,reason,self.smoothed,bounds)
