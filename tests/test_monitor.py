import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import norm, rate, phase_for_minutes, build_races, build_win_odds, rank_bets, load_zip

class MonitorTest(unittest.TestCase):
    def test_basics(self):
        self.assertEqual(norm(' 単 勝 '), '単勝')
        self.assertAlmostEqual(rate('3-2-0-5'), 4/20)
        self.assertIsNone(phase_for_minutes(35))
        self.assertEqual(phase_for_minutes(12), '事前')
        self.assertEqual(phase_for_minutes(3), '直前')
    def test_fixture(self):
        base=Path(__file__).parent
        races_raw,_ = load_zip('', str(base/'sample_race.zip'))
        odds_raw,_ = load_zip('', str(base/'sample_odds.zip'))
        races=build_races(races_raw['races'], races_raw['horses'], '20261027')
        odds=build_win_odds(odds_raw['odds'], '20261027')
        self.assertEqual(len(races), 2)
        self.assertEqual(len(races[1]['horses']), 4)
        self.assertEqual(len(odds[1]),4)
        bets,ranked=rank_bets(races[1],odds[1])
        self.assertEqual(len(ranked),4)
        self.assertTrue(all('proxy' in v for v in ranked))

if __name__=='__main__': unittest.main()
