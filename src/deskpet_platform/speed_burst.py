"""Detect token-speed surges with the rules of the reference display firmware."""


class SpeedBurst:
    DURATION_MS = 5000
    RECOVERY_MS = 20000
    SAMPLE_GAP_MS = 2500

    def __init__(self):
        self.recent = [0] * 5
        self.count = self.next = self.rise_baseline = 0
        self.sampled = self.bursting = self.peak_tracking = False
        self.report_at = 0
        self.sample_id = None
        self.burst_at = None
        self.before = self.peak = 0

    def source_changed(self):
        self.sampled = self.peak_tracking = False
        self.count = self.next = self.rise_baseline = 0

    def report(self, sample_id, rate10, state, gear, now_ms):
        continuous = self.sampled and now_ms - self.report_at <= self.SAMPLE_GAP_MS
        if continuous and sample_id == self.sample_id:
            return
        if not continuous:
            self.source_changed()
        self.sampled, self.report_at, self.sample_id = True, now_ms, sample_id
        if not rate10 or state not in ("L", "E"):
            self.count = self.next = self.rise_baseline = 0
            return
        cooling = self.burst_at is not None and now_ms - self.burst_at < self.DURATION_MS + self.RECOVERY_MS
        if self.bursting:
            if self.peak_tracking and self.peak < rate10:
                self.peak = rate10
            self.rise_baseline = 0
        elif self.count < 5 or gear < 4 or cooling:
            self.rise_baseline = 0
        elif self.rise_baseline and 25 * rate10 >= 6 * self.rise_baseline:
            self.before = (self.rise_baseline + 2) // 5
            self.peak = rate10
            self.peak_tracking = self.bursting = True
            self.burst_at, self.rise_baseline = now_ms, 0
        else:
            total = sum(self.recent)
            self.rise_baseline = total if 25 * rate10 >= 6 * total else 0
        self.recent[self.next] = rate10
        self.next = (self.next + 1) % 5
        self.count = min(self.count + 1, 5)

    def update(self, gear, now_ms):
        if gear < 4:
            self.rise_baseline = 0
        if self.bursting and now_ms - self.burst_at >= self.DURATION_MS:
            self.bursting = self.peak_tracking = False
            self.before = self.peak = 0

    def wire(self, now_ms):
        if not self.bursting or now_ms - self.burst_at >= self.DURATION_MS:
            return {}
        fields = {"burst_ms": now_ms - self.burst_at}
        if self.before and self.peak > self.before:
            fields["surge"] = min(999, ((self.peak - self.before) * 100 + self.before // 2) // self.before)
        return fields
