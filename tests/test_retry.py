import contextlib
import io
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m


def http_error(code):
    return urllib.error.HTTPError('https://example.invalid/private?token=secret', code, 'failure', {}, None)


class RetryTest(unittest.TestCase):
    def test_retryable_errors_wait_once_then_succeed(self):
        errors = [http_error(c) for c in (404, 408, 429, 500, 502, 503, 504)]
        errors += [urllib.error.URLError('unavailable'), TimeoutError(), ConnectionResetError()]
        for error in errors:
            with self.subTest(error=error), patch.object(m, 'load_zip', side_effect=[error, ({}, None)]) as fetch, patch.object(m.time, 'sleep') as sleep, contextlib.redirect_stderr(io.StringIO()) as log:
                self.assertEqual(m.load_source('odds', 'url'), ({}, None))
                self.assertEqual(fetch.call_count, 2)
                sleep.assert_called_once_with(120)
                self.assertIn('odds:', log.getvalue())
                self.assertNotIn('secret', log.getvalue())

    def test_exhausted_retry_is_bounded_and_labelled(self):
        for label in ('race', 'odds'):
            with self.subTest(label=label), patch.object(m, 'load_zip', side_effect=http_error(404)) as fetch, patch.object(m.time, 'sleep') as sleep, contextlib.redirect_stderr(io.StringIO()) as log:
                with self.assertRaisesRegex(RuntimeError, label + ': HTTP 404'):
                    m.load_source(label, 'url')
                self.assertEqual(fetch.call_count, 2)
                sleep.assert_called_once_with(120)
                self.assertIn('attempt 2/2', log.getvalue())

    def test_permanent_http_failures_do_not_retry(self):
        for code in (400, 401, 403, 410):
            with self.subTest(code=code), patch.object(m, 'load_zip', side_effect=http_error(code)) as fetch, patch.object(m.time, 'sleep') as sleep, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, f'race: HTTP {code}'):
                    m.load_source('race', 'url')
                fetch.assert_called_once()
                sleep.assert_not_called()

    def test_no_retry_when_global_time_budget_is_too_small(self):
        budget = {'remaining': 1, 'deadline': 240}
        with patch.object(m.time, 'monotonic', return_value=96), patch.object(m, 'load_zip', side_effect=http_error(404)) as fetch, patch.object(m.time, 'sleep') as sleep, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                m.load_source('odds', 'url', retry_budget=budget)
        fetch.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(budget['remaining'], 1)

    def test_oversleep_does_not_start_request_beyond_deadline(self):
        budget = {'remaining': 1, 'deadline': 240}
        with patch.object(m.time, 'monotonic', side_effect=[0, 0, 241]), patch.object(m, 'load_zip', side_effect=http_error(404)) as fetch, patch.object(m.time, 'sleep') as sleep, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '時間予算'):
                m.load_source('odds', 'url', retry_budget=budget)
        fetch.assert_called_once()
        sleep.assert_called_once_with(120)

    def test_invalid_zip_and_local_failure_do_not_retry(self):
        for error, local in ((ValueError('not ZIP'), ''), (FileNotFoundError(), 'missing.zip')):
            with self.subTest(error=error), patch.object(m, 'load_zip', side_effect=error) as fetch, patch.object(m.time, 'sleep') as sleep:
                with self.assertRaises(type(error)):
                    m.load_source('race', 'url', local)
                fetch.assert_called_once()
                sleep.assert_not_called()


class RunSafetyTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 29, 14, 0, tzinfo=m.TZ)
        self.clock_time = self.now
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m, 'DEMO_MODE', False))
        self.stack.enter_context(patch.object(m, 'LOCAL_RACE_ZIP', ''))
        self.stack.enter_context(patch.object(m, 'LOCAL_ODDS_ZIP', ''))
        self.notify = self.stack.enter_context(patch.object(m, 'notify'))
        self.save = self.stack.enter_context(patch.object(m, 'save_state'))
        self.state = self.stack.enter_context(patch.object(m, 'load_state', return_value={}))
        self.stack.enter_context(patch.object(m.urllib.request, 'urlopen', side_effect=AssertionError('live network forbidden')))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.race = {'no': 1, 'start': self.now + timedelta(minutes=11), 'name': 'test', 'horses': {}}
        self.stack.enter_context(patch.object(m, 'build_races', return_value={1: self.race}))
        self.stack.enter_context(patch.object(m, 'build_win_odds', return_value={1: {}}))
        self.stack.enter_context(patch.object(m, 'rank_bets', return_value=([], [{}])))

    def clock(self):
        return self.clock_time

    def sleep(self, seconds):
        self.clock_time += timedelta(seconds=seconds)

    def test_retry_recalculates_phase_and_records_new_time(self):
        with patch.object(m, 'load_zip', side_effect=[({}, None), http_error(404), ({}, self.now)]), patch.object(m.time, 'sleep', side_effect=self.sleep):
            m.run(clock=self.clock)
        self.notify.assert_called_once()
        self.assertIn('直前予想', self.notify.call_args.args[0])
        self.assertIn('取得判定: 14:02:00', self.notify.call_args.args[0])
        self.assertIn('1R-直前', self.save.call_args.args[0]['sent'])

    def test_retry_past_start_or_last_minute_does_not_notify(self):
        for minutes in (1, 2, 2.5):
            with self.subTest(minutes=minutes):
                self.clock_time = self.now
                self.race['start'] = self.now + timedelta(minutes=minutes)
                with patch.object(m, 'load_zip', side_effect=[({}, None), http_error(503), ({}, self.now)]), patch.object(m.time, 'sleep', side_effect=self.sleep):
                    m.run(clock=self.clock)
                self.notify.assert_not_called()
                self.save.assert_not_called()

    def test_retry_can_make_odds_stale(self):
        with patch.object(m, 'load_zip', side_effect=[({}, None), http_error(404), ({}, self.now - timedelta(minutes=7))]), patch.object(m.time, 'sleep', side_effect=self.sleep):
            with self.assertRaisesRegex(RuntimeError, '古い'):
                m.run(clock=self.clock)
        self.notify.assert_not_called()
        self.save.assert_not_called()
        self.state.assert_not_called()

    def test_failed_source_does_not_notify_or_access_state(self):
        for failures in ([http_error(404), http_error(404)], [({}, None), http_error(503), http_error(503)]):
            with self.subTest(failures=failures), patch.object(m, 'load_zip', side_effect=failures), patch.object(m.time, 'sleep', side_effect=self.sleep):
                with self.assertRaises(RuntimeError):
                    m.run(clock=self.clock)
                self.notify.assert_not_called()
                self.save.assert_not_called()
                self.state.assert_not_called()

    def test_two_source_failures_share_only_one_retry(self):
        with patch.object(m, 'load_zip', side_effect=[http_error(404), ({}, None), http_error(503)]) as fetch, patch.object(m.time, 'sleep', side_effect=self.sleep) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'odds: HTTP 503'):
                m.run(clock=self.clock)
        self.assertEqual(fetch.call_count, 3)
        sleep.assert_called_once_with(120)
        self.notify.assert_not_called()
        self.save.assert_not_called()
        self.state.assert_not_called()

    def test_race_retry_leaves_successful_odds_fetch_available(self):
        with patch.object(m, 'load_zip', side_effect=[http_error(404), ({}, None), ({}, self.now)]) as fetch, patch.object(m.time, 'sleep', side_effect=self.sleep) as sleep:
            m.run(clock=self.clock)
        self.assertEqual(fetch.call_count, 3)
        sleep.assert_called_once_with(120)
        self.notify.assert_called_once()
        self.save.assert_called_once()

    def test_missing_timestamp_does_not_notify(self):
        with patch.object(m, 'load_zip', side_effect=[({}, None), ({}, None)]):
            with self.assertRaisesRegex(RuntimeError, '配信時刻'):
                m.run(clock=self.clock)
        self.notify.assert_not_called()
        self.save.assert_not_called()

    def test_freshness_threshold_is_preserved(self):
        for age, accepted in ((8, True), (8.01, False)):
            with self.subTest(age=age), patch.object(m, 'load_zip', side_effect=[({}, None), ({}, self.now - timedelta(minutes=age))]):
                self.notify.reset_mock()
                self.save.reset_mock()
                if accepted:
                    m.run(clock=self.clock)
                    self.notify.assert_called_once()
                else:
                    with self.assertRaisesRegex(RuntimeError, '古い'):
                        m.run(clock=self.clock)
                    self.notify.assert_not_called()
                    self.save.assert_not_called()

    def test_post_fetch_date_is_used_after_midnight(self):
        self.clock_time = self.now.replace(hour=23, minute=59)
        with patch.object(m, 'load_zip', side_effect=[({}, None), http_error(404), ({}, self.clock_time)]), patch.object(m.time, 'sleep', side_effect=self.sleep), patch.object(m, 'build_races', return_value={}) as build:
            m.run(clock=self.clock)
        self.assertEqual(build.call_args.args[2], '20261030')
        self.notify.assert_not_called()
        self.save.assert_not_called()

    def test_phase_boundaries_unchanged(self):
        for minutes, expected in ((20.01, None), (20, '事前'), (10, '事前'), (9.99, '直前'), (1, '直前'), (.99, None), (0, None), (-1, None)):
            with self.subTest(minutes=minutes):
                self.assertEqual(m.phase_for_minutes(minutes), expected)


if __name__ == '__main__':
    unittest.main()
