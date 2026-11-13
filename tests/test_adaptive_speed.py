import tempfile
import unittest
from pathlib import Path
from deskpet_platform.adaptive_speed.history import History,Frame,quantile


class SpeedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.now=1_800_000_000.
        self.path=Path(self.folder.name)/'speed.sqlite3';self.history=History(self.path)
    def tearDown(self):self.history.close();self.folder.cleanup()
    def frame(self, key, offset=0, rate10=500, status='L',version='output-v1',demo=False):
        return Frame(key,self.now+offset,rate10,status,version,demo)

    def test_same_rate_in_different_seconds_counts_but_packet_retry_does_not(self):
        first=self.frame('one')
        self.assertEqual(self.history.add(first,self.now),'positive')
        self.assertEqual(self.history.add(first,self.now+1),'duplicate')
        self.assertEqual(self.history.add(self.frame('same-second',.3),self.now+.3),'same_second')
        self.assertEqual(self.history.add(self.frame('two',1),self.now+1),'positive')
        self.assertEqual(self.history.window('output-v1',self.now+2).recent.count,2)

    def test_zero_missing_idle_demo_and_stale_do_not_pollute_positive_distribution(self):
        for i,(rate,status) in enumerate([(0,'L'),(500,'I'),(1000,'N'),(0,'W')]):
            self.history.add(self.frame(str(i),i,rate,status),self.now+i)
        self.assertEqual(self.history.add(self.frame('demo',4,demo=True),self.now+4),'demo')
        self.assertEqual(self.history.add(self.frame('old',0),self.now+10),'stale')
        result=self.history.window('output-v1',self.now+10)
        self.assertEqual(result.recent.count,0)
        self.assertEqual(result.diagnostics['zero_seconds'],1)
        self.assertEqual(result.diagnostics['reported_missing_seconds'],2)
        self.assertEqual(result.diagnostics['missing_seconds'],8)
        self.assertEqual(result.diagnostics['idle_seconds'],1)

    def test_current_sample_excluded_and_recent_does_not_leak_into_long_term(self):
        self.history.add(self.frame('long',-2000,rate10=100),self.now-2000)
        self.history.add(self.frame('recent',-100,rate10=500),self.now-100)
        self.history.add(self.frame('current',0,rate10=9999999),self.now)
        result=self.history.window('output-v1',self.now)
        self.assertEqual(result.recent.median,50)
        self.assertEqual(result.long.median,10)
        self.assertEqual((result.recent.count,result.long.count),(1,1))

    def test_version_restart_and_corrupt_history_recovery(self):
        self.history.add(self.frame('a'),self.now)
        self.history.add(self.frame('b',1,rate10=1000,version='output-v2'),self.now+1)
        self.history.close();self.history=History(self.path)
        self.assertEqual(self.history.window('output-v1',self.now+2).recent.median,50)
        self.assertEqual(self.history.window('output-v2',self.now+2).recent.median,100)
        self.history.close();self.path.write_bytes(b'broken')
        self.history=History(self.path)
        self.assertTrue(self.history.recovered)
        self.assertEqual(self.history.window('output-v1',self.now+2).recent.count,0)

    def test_exact_linear_quantiles_ties_and_finite_validation(self):
        self.assertEqual(quantile([1,2,3,4],.25),1.75)
        self.assertEqual(quantile([50]*10,.9),50)
        self.assertIsNone(quantile([],.5))
        self.assertEqual(self.history.add(self.frame('nan',rate10=float('nan')),self.now),'invalid')
        self.assertEqual(self.history.add(self.frame('future',10),self.now),'future')

    def test_fractional_current_second_is_excluded_and_silent_gaps_are_missing(self):
        self.history.add(self.frame('old',-2),self.now-2)
        self.history.add(self.frame('current',.2,rate10=1000000),self.now+.2)
        result=self.history.window('output-v1',self.now+.7)
        self.assertEqual(result.recent.count,1)
        self.assertEqual(result.recent.median,50)
        self.assertEqual(result.diagnostics['unobserved_seconds'],1)

    def test_long_warmup_requires_two_hours_and_two_distinct_use_periods(self):
        rows=[]
        for start in (self.now-12000,self.now-6000):
            for i in range(3600):rows.append(('output-v1',int(start+i),f'{start}:{i}',500+i%100,'L','positive'))
        for i in range(300):rows.append(('output-v1',int(self.now-300+i),f'recent:{i}',300+i%100,'L','positive'))
        with self.history.db:self.history.db.executemany('INSERT INTO speed_sample VALUES(?,?,?,?,?,?)',rows)
        state=self.history.window('output-v1',self.now)
        self.assertTrue(state.recent_ready);self.assertTrue(state.long_ready)
        self.assertEqual(state.long.count,7200);self.assertEqual(state.periods,2)
        with self.history.db:self.history.db.execute("DELETE FROM speed_sample WHERE sample_id=?",(rows[0][2],))
        self.assertFalse(self.history.window('output-v1',self.now).long_ready)


class HistoryRecoveryTests(unittest.TestCase):
    def test_locked_healthy_history_is_preserved_and_opens_after_lock_is_released(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'history.sqlite3';history=History(path)
            history.add(Frame('one',1800000000,500,'L','v1'),1800000000);history.close()
            lock=sqlite3.connect(path);opened=None
            try:
                lock.execute('PRAGMA locking_mode=EXCLUSIVE');lock.execute('BEGIN EXCLUSIVE')
                lock.execute('SELECT * FROM speed_sample').fetchall()
                with self.assertRaises(sqlite3.OperationalError):opened=History(path)
                self.assertTrue(path.exists());self.assertEqual(list(Path(folder).glob('*.corrupt-*')),[])
            finally:
                if opened:opened.close()
                lock.rollback();lock.close()
            reopened=History(path)
            try:self.assertEqual(reopened.window('v1',1800000001).recent.count,1)
            finally:reopened.close()

    def test_non_corruption_open_errors_never_rename_existing_history(self):
        import sqlite3
        from unittest.mock import patch
        for code in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_CANTOPEN,sqlite3.SQLITE_IOERR,sqlite3.SQLITE_FULL):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'history.sqlite3';history=History(path);history.close();before=path.read_bytes()
                error=sqlite3.OperationalError('injected non-corruption error');error.sqlite_errorcode=code
                with patch('deskpet_platform.adaptive_speed.history.sqlite3.connect',side_effect=error):
                    with self.assertRaises(sqlite3.OperationalError):History(path)
                self.assertTrue(path.exists());self.assertEqual(path.read_bytes(),before)
                self.assertEqual(list(Path(folder).glob('*.corrupt-*')),[])
