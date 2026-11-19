import unittest

from deskpet_platform.speed_burst import SpeedBurst


class SpeedBurstTests(unittest.TestCase):
    def feed(self, burst, rates, *, start=0, gear=5, state="L", step=1000):
        now = start
        for index, rate in enumerate(rates):
            now = start + index * step
            burst.update(gear, now)
            burst.report(now, rate, state, gear, now)
        return now

    def test_two_readings_above_the_recent_mean_start_a_bounded_burst(self):
        burst = SpeedBurst()
        now = self.feed(burst, [100] * 5 + [130])
        self.assertEqual(burst.wire(now), {})
        now = self.feed(burst, [125], start=now + 1000)
        self.assertEqual(burst.wire(now), {"burst_ms": 0, "surge": 25})
        now = self.feed(burst, [180], start=now + 1000)
        self.assertEqual(burst.wire(now), {"burst_ms": 1000, "surge": 80})
        burst.update(5, now + 4000)
        self.assertEqual(burst.wire(now + 4000), {})

    def test_low_gear_idle_and_gaps_never_start_a_burst(self):
        for kwargs, rates in (({"gear": 3}, [100] * 5 + [140, 140]),
                              ({"state": "I"}, [100] * 5 + [140, 140]),
                              ({"step": 3000}, [100] * 5 + [140, 140])):
            burst = SpeedBurst()
            now = self.feed(burst, rates, **kwargs)
            self.assertEqual(burst.wire(now), {}, kwargs)

    def test_repeated_samples_are_counted_once(self):
        burst = SpeedBurst()
        now = self.feed(burst, [100] * 5 + [140])
        burst.report(now, 140, "L", 5, now + 500)
        self.assertEqual(burst.wire(now + 500), {})

    def test_recovery_period_follows_each_burst(self):
        burst = SpeedBurst()
        now = self.feed(burst, [100] * 5 + [140, 140])
        self.assertTrue(burst.wire(now))
        now = self.feed(burst, [140] * 10 + [200, 200], start=now + 1000)
        self.assertEqual(burst.wire(now), {})
        now = self.feed(burst, [140] * 13 + [200, 200], start=now + 1000)
        self.assertEqual(burst.wire(now)["burst_ms"], 0)


if __name__ == "__main__":
    unittest.main()
