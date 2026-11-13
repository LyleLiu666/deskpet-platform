import unittest
from deskpet_platform.adaptive_speed.history import Distribution,Window
from deskpet_platform.adaptive_speed.classifier import Classifier


def window(recent=None, baseline=None, ready=True):
    recent=recent if recent is not None else list(range(10,110))*3
    baseline=baseline if baseline is not None else list(range(10,110))*80
    return Window(Distribution.of(recent),Distribution.of(baseline),2,{},ready,ready)


class AdaptiveClassifierTests(unittest.TestCase):
    def test_narrow_distribution_stays_in_middle_instead_of_cycling_five_tiers(self):
        c=Classifier();w=window(recent=[49,50,51]*100)
        tiers=[]
        for second in range(40):tiers.append(c.update(49+(second%3),second,w).current)
        self.assertEqual(set(tiers[4:]),{3})

    def test_overall_high_and_current_slow_are_independent(self):
        c=Classifier();w=window(recent=[90]*300)
        c.update(10,0,w);state=c.update(10,60,w)
        self.assertEqual(state.overall,3)
        self.assertLess(state.current,3)
        self.assertTrue(state.calibrated)

    def test_overall_low_and_current_fast_does_not_create_starlight(self):
        c=Classifier();w=window(recent=[20]*300)
        c.update(100,0,w);state=c.update(100,2,w)
        self.assertEqual(state.overall,0)
        self.assertEqual(state.current,5)

    def test_overall_requires_two_minute_judgments_and_drop_does_not_shift_own_baseline(self):
        c=Classifier();high=window(recent=[95]*300);low=window(recent=[15]*300)
        self.assertEqual(c.update(95,0,high).overall,0)
        self.assertEqual(c.update(95,59,high).overall,0)
        self.assertEqual(c.update(95,60,high).overall,3)
        self.assertEqual(c.update(15,120,low).overall,3)
        self.assertEqual(c.update(15,180,low).overall,0)

    def test_zero_missing_and_expired_warmup_hide_environment_immediately(self):
        c=Classifier();w=window(recent=[95]*300)
        c.update(95,0,w);self.assertEqual(c.update(95,60,w).overall,3)
        self.assertEqual(c.update(0,61,w).current,0)
        self.assertEqual(c.update(0,61,w).overall,0)
        self.assertEqual(c.update(None,62,w).reason,'missing')
        state=c.update(95,63,window(ready=False))
        self.assertFalse(state.calibrated);self.assertEqual(state.overall,0)

    def test_constant_baseline_has_no_fake_extreme_environment(self):
        c=Classifier();w=window(recent=[50]*300,baseline=[50]*8000)
        c.update(50,0,w);s=c.update(50,60,w)
        self.assertEqual(s.overall,0);self.assertFalse(s.calibrated)
        self.assertEqual(s.reason,'narrow_baseline')

    def test_single_extreme_does_not_change_median_or_minute_level(self):
        c=Classifier();w=window(recent=list(range(30,60))*10)
        c.update(45,0,w);before=c.update(45,60,w).overall
        spike=window(recent=list(range(30,60))*10+[1000000])
        self.assertEqual(c.update(1000000,61,spike).overall,before)

    def test_tied_quantile_boundaries_use_stable_middle_and_uncalibrated_environment(self):
        c=Classifier();w=window(recent=[10]*240+[20]*60,baseline=([10]*240+[20]*60)*30)
        c.update(10,0,w);state=c.update(10,60,w)
        self.assertEqual(state.current,3)
        self.assertEqual(state.overall,0);self.assertFalse(state.calibrated)
        self.assertEqual(state.reason,'tied_baseline')

    def test_product_example_cyan_environment_with_slow_fourth_and_fifth_motion(self):
        # README §4.4: long Q80=140, recent median=160, motion boundaries 120/145/175/195.
        recent=Distribution(300,80,100,120,125,145,160,175,185,195,210)
        baseline=Distribution(8000,20,40,60,70,90,100,120,140,160,220)
        w=Window(recent,baseline,2,{},True,True)
        for speed,tier in ((110,1),(185,4),(200,5)):
            with self.subTest(speed=speed):
                c=Classifier();c.update(speed,0,w);state=c.update(speed,60,w)
                self.assertEqual(state.overall,3)
                self.assertEqual(state.current,tier)
                self.assertEqual(state.boundaries,(120,145,175,195))

    def test_product_overall_table_uses_inclusive_lower_boundaries(self):
        baseline=Distribution(8000,20,40,60,70,90,100,120,140,160,220)
        for median,level in ((69,0),(70,1),(99,1),(100,2),(139,2),(140,3)):
            with self.subTest(median=median):
                w=Window(Distribution.of([median]*300),baseline,2,{},True,True)
                c=Classifier();c.update(median,0,w);state=c.update(median,60,w)
                self.assertEqual(state.overall,level)

    def test_product_exponential_formula_with_uneven_elapsed_seconds(self):
        import math
        c=Classifier();w=window()
        c.update(100,0,w)
        self.assertAlmostEqual(c.update(160,1,w).smoothed,100+60*(1-math.exp(-1/3)))
        self.assertAlmostEqual(c.update(160,4,w).smoothed,160-60*math.exp(-4/3))

    def test_multilevel_hysteresis_stops_at_intermediate_level_instead_of_old_extreme(self):
        baseline=Distribution(8000,20,40,60,70,90,100,120,140,160,220)
        for initial,changed,expected in ((60,101,1),(160,97,2),(60,146,3),(160,64,0)):
            with self.subTest(initial=initial,changed=changed):
                c=Classifier()
                before=Window(Distribution.of([initial]*300),baseline,2,{},True,True)
                after=Window(Distribution.of([changed]*300),baseline,2,{},True,True)
                c.update(initial,0,before);c.update(initial,60,before)
                c.update(changed,120,after)
                self.assertEqual(c.update(changed,180,after).overall,expected)
