"""大井競馬 直前オッズ監視・紙上買い目通知。追加Pythonパッケージ不要。"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Tokyo')
RACE_URL = 'https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload?type=daily'
ODDS_URL = 'https://www.keiba.go.jp/KeibaWeb/DataDownload/OddsDataDownload?type=daily'
VENUE = '大井'
STATE_PATH = Path(os.getenv('STATE_PATH', 'state.json'))
DEMO_MODE = '--demo' in sys.argv
LOCAL_RACE_ZIP = os.getenv('LOCAL_RACE_ZIP', '')
LOCAL_ODDS_ZIP = os.getenv('LOCAL_ODDS_ZIP', '')


def norm(value):
    return unicodedata.normalize('NFKC', str(value or '')).strip().replace(' ', '').replace('　', '')


def to_num(value, default=None):
    try:
        num = float(norm(value).replace(',', ''))
        return num if math.isfinite(num) else default
    except (TypeError, ValueError):
        return default


def to_int(value, default=None):
    num = to_num(value)
    return int(num) if num is not None else default


def http_get(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'NarOiResearchMonitor/0.1 (personal odds analysis; 5min schedule)',
        'Accept': 'application/zip,application/octet-stream',
    })
    with urllib.request.urlopen(req, timeout=25) as response:
        data = response.read(80 * 1024 * 1024 + 1)
        if len(data) > 80 * 1024 * 1024:
            raise ValueError('ZIP が80MBを超えました。処理せず停止します')
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ValueError(f'公式データのダウンロード結果がZIPではありません: {url}')
        return data, response.headers.get('Content-Disposition', '')


def load_zip(source_url, local_file=''):
    if local_file:
        data = Path(local_file).read_bytes()
        disposition = Path(local_file).name
    else:
        data, disposition = http_get(source_url)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError('ZIP形式ではないデータを受信しました')
    archive = zipfile.ZipFile(io.BytesIO(data))
    rows_by_kind = {}
    for name in archive.namelist():
        base = name.split('/')[-1].lower()
        if not base.endswith('.csv'):
            continue
        if 'racelist' in base:
            kind = 'races'
        elif 'horselist' in base:
            kind = 'horses'
        elif 'odds' in base:
            kind = 'odds'
        else:
            continue
        raw = archive.read(name)
        parsed = None
        for encoding in ('utf-8-sig', 'cp932'):
            try:
                parsed = list(csv.reader(io.StringIO(raw.decode(encoding))))
                break
            except UnicodeDecodeError:
                pass
        if parsed is None:
            raise ValueError(f'CSVをデコードできません: {name}')
        # 公開データは列ヘッダーがある場合・ない場合の両方を許容。
        if parsed and norm(parsed[0][0]) in ('競馬場', '競馬場名'):
            parsed = parsed[1:]
        rows_by_kind[kind] = parsed
    filename_match = re.search(r'(20\d{6})_(\d{10})_(?:odds|race)\.zip', disposition)
    published_at = datetime.fromtimestamp(int(filename_match.group(2)), TZ) if filename_match else None
    return rows_by_kind, published_at


def load_source(label, source_url, local_file='', *, retry_budget=None):
    """Share one retry and a 240-second fetch budget across both sources.

    The deadline governs request admission, not a hard cancellation of urllib I/O.
    Reserve 25 seconds for the next request and 60 seconds of the five-minute job
    for parsing/notifications/state and runner overhead.
    """
    if retry_budget is None:
        retry_budget = {'remaining': 1, 'deadline': time.monotonic() + 240}
    for attempt in range(2):
        if time.monotonic() + 25 > retry_budget['deadline']:
            raise RuntimeError(f'{label}: 取得時間予算不足のため通知しません')
        try:
            return load_zip(source_url, local_file)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                detail = f'HTTP {exc.code}'
                transient = exc.code in (404, 408, 429) or 500 <= exc.code <= 599
            else:
                detail = type(exc).__name__
                transient = True
            # Do not expose custom URL credentials/query strings in logs.
            print(f'{label}: {detail} (attempt {attempt + 1}/2)', file=sys.stderr)
            if (local_file or not transient or attempt == 1
                    or retry_budget['remaining'] == 0
                    or time.monotonic() + 120 + 25 > retry_budget['deadline']):
                raise RuntimeError(f'{label}: {detail}; 取得失敗のため通知しません') from exc
            retry_budget['remaining'] -= 1
            print(f'{label}: 120秒待機して1回だけ再取得します', file=sys.stderr)
            time.sleep(120)


def build_races(race_rows, horse_rows, date_key):
    race_map = {}
    for row in race_rows:
        if len(row) < 4 or norm(row[0]) != VENUE or norm(row[1]) != date_key:
            continue
        race_no = to_int(row[2])
        if not race_no:
            continue
        hhmm = norm(row[3]).zfill(4)
        if not re.fullmatch(r'\d{4}', hhmm):
            continue
        try:
            dt = datetime.strptime(date_key + hhmm, '%Y%m%d%H%M').replace(tzinfo=TZ)
        except ValueError:
            continue
        race_map[race_no] = {'no': race_no, 'start': dt, 'name': row[5] if len(row) > 5 else '', 'horses': {}}
    for row in horse_rows:
        if len(row) < 7 or norm(row[0]) != VENUE or norm(row[1]) != date_key:
            continue
        race_no, horse_no = to_int(row[2]), to_int(row[5])
        if not race_no or not horse_no or race_no not in race_map:
            continue
        # 当日の未確定結果は使わない。既往成績だけ利用する。
        race_map[race_no]['horses'][horse_no] = {
            'no': horse_no, 'name': row[6],
            'jockey': row[14] if len(row) > 14 else '',
            'jockey_form': row[17] if len(row) > 17 else '',
            'career_form': row[24] if len(row) > 24 else '',
            'venue_form': row[27] if len(row) > 27 else '',
            'distance_form': row[28] if len(row) > 28 else '',
        }
    return race_map


def build_win_odds(odds_rows, date_key):
    result = {}
    for row in odds_rows:
        if len(row) < 8 or norm(row[0]) != VENUE or norm(row[1]) != date_key:
            continue
        if norm(row[3]) not in ('単勝', '単勝式'):
            continue
        race_no, horse_no = to_int(row[2]), to_int(row[4])
        odd = to_num(row[7])
        if race_no and horse_no and odd and odd >= 1.0:
            result.setdefault(race_no, {})[horse_no] = odd
    return result


def rate(record):
    """'1-2-3-5' を過去の出走成績として保守的に平滑化。学習済モデルではない。"""
    parts = [to_int(p) for p in re.findall(r'\d+', norm(record))]
    if len(parts) < 4:
        return None
    n = sum(parts[:4])
    return (parts[0] + 1) / (n + 10) if n > 0 else None


def rank_bets(race, odds):
    """未検証の仮評価。オッズ市場に形式上の加点を加えた参考指標。"""
    candidates = []
    for number, horse in race['horses'].items():
        odd = odds.get(number)
        if not odd:
            continue
        components = []
        for field, weight in (('career_form', .35), ('venue_form', .20), ('distance_form', .30), ('jockey_form', .15)):
            val = rate(horse[field])
            if val is not None:
                components.append((val, weight))
        form = (sum(v*w for v, w in components) / sum(w for _, w in components)) if components else None
        candidates.append({'no': number, 'name': horse['name'], 'odds': odd, 'form': form})
    if len(candidates) < 3:
        return [], []
    market_total = sum(1 / c['odds'] for c in candidates)
    # 馬の基礎指数が全頭欠けたレースでは推奨を出さない。
    if not all(c['form'] is not None for c in candidates):
        return [], candidates
    form_total = sum(c['form'] for c in candidates)
    for c in candidates:
        market = (1/c['odds']) / market_total
        shape = c['form'] / form_total if form_total else market
        c['index'] = 0.80*market + 0.20*shape
        c['proxy'] = c['index']*c['odds']
        # proxy値は期待回収率ではない。固定係数・非較正モデルの参考値。
    ranked = sorted(candidates, key=lambda c: (c['proxy'], c['index']), reverse=True)
    bets = [c for c in ranked if 2.0 <= c['odds'] <= 20.0 and c['proxy'] >= 1.08][:2]
    return bets, ranked


def load_state():
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
            return state if isinstance(state, dict) else {}
        except (ValueError, OSError):
            pass
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STATE_PATH)


def notify(text):
    print(text)
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '')
    if DEMO_MODE or not webhook:
        if not DEMO_MODE:
            raise RuntimeError('DISCORD_WEBHOOK_URL が未設定です。通知は送れません')
        return
    body = json.dumps({'content': text, 'allowed_mentions': {'parse': []}}).encode('utf-8')
    req = urllib.request.Request(webhook, data=body, method='POST', headers={'Content-Type':'application/json', 'User-Agent':'NarOiResearchMonitor/0.1'})
    with urllib.request.urlopen(req, timeout=15) as res:
        if res.status not in (200, 204):
            raise RuntimeError(f'Discord 通知失敗: HTTP {res.status}')


def phase_for_minutes(minutes):
    if 10 <= minutes <= 20:
        return '事前'
    if 1.0 <= minutes <= 9.99:
        return '直前'
    return None


def run(now=None, *, clock=None):
    # Explicit time is retained for the offline demo; production uses a live clock.
    clock = clock or ((lambda fixed=now: fixed) if now is not None else (lambda: datetime.now(TZ)))
    retry_budget = {'remaining': 1, 'deadline': time.monotonic() + 240}
    races_raw, _ = load_source('race', os.getenv('NAR_RACE_ZIP_URL', RACE_URL), LOCAL_RACE_ZIP, retry_budget=retry_budget)
    odds_raw, odds_at = load_source('odds', os.getenv('NAR_ODDS_ZIP_URL', ODDS_URL), LOCAL_ODDS_ZIP, retry_budget=retry_budget)
    now = clock()
    date_key = now.strftime('%Y%m%d')
    races = build_races(races_raw.get('races', []), races_raw.get('horses', []), date_key)
    odds = build_win_odds(odds_raw.get('odds', []), date_key)
    if not races:
        print(f'大井の開催データなし: {date_key}')
        return
    # 公式ZIPのタイムスタンプを取得できなければ「直前」と断定しない。
    if not DEMO_MODE and odds_at is None:
        raise RuntimeError('オッズZIPの配信時刻を確認できません。古いオッズを避けるため通知しません')
    if odds_at and now - odds_at > timedelta(minutes=8):
        raise RuntimeError(f'オッズデータが古いため停止: {odds_at.isoformat()}')
    state = {} if DEMO_MODE else load_state()
    if state.get('date') != date_key:
        state = {'date': date_key, 'sent': {}, 'last_picks': {}}
    pending = []
    for no, race in sorted(races.items()):
        minutes = (race['start']-now).total_seconds()/60
        phase = phase_for_minutes(minutes)
        if not phase:
            continue
        key = f'{no}R-{phase}'
        if state['sent'].get(key):
            continue
        win_odds = odds.get(no, {})
        bets, ranked = rank_bets(race, win_odds)
        if not ranked:
            print(f'{no}R: オッズ・出馬表不完全のため見送り')
            continue
        if bets:
            lines = [f"{c['no']}番 {c['name']} / 単勝 {c['odds']:.1f}倍 / 参考指数 {c['proxy']:.2f}" for c in bets]
            picks = ','.join(str(c['no']) for c in bets)
            result = '\n'.join(lines) + f'\n紙上買い目：単勝 各100円（合計{100*len(bets)}円）'
        else:
            picks = '見送り'
            result = '条件を満たす候補なし → 見送り（紙上判定）'
        previous = state['last_picks'].get(str(no))
        changed = '（前回から変更あり）' if previous and previous != picks else ''
        ts = odds_at.strftime('%H:%M:%S') if odds_at else 'デモデータ'
        message = (f'🏇 大井 {no}R {race["start"].strftime("%H:%M")}発走｜{phase}予想 {changed}\n'
                   f'{race["name"]}\n'
                   f'オッズZIP生成時刻: {ts} / 取得判定: {now.strftime("%H:%M:%S")}\n'
                   f'{result}\n'
                   '※未学習の試作指数です。期待回収率ではなく、的中や利益は保証しません。自動購入は行いません。')
        # 通知成功後だけ送信済みと記録する。
        notify(message)
        state['sent'][key] = now.isoformat()
        state['last_picks'][str(no)] = picks
        pending.append(no)
        if not DEMO_MODE:
            save_state(state)
    if not pending:
        print(f'監視完了：{date_key} {now.strftime("%H:%M")}, 対象通知なし')


if __name__ == '__main__':
    try:
        if DEMO_MODE:
            # 明示的なデモ日付。サンプルCSVはこの日付と時刻に合わせている。
            run(datetime(2026, 10, 27, 14, 16, tzinfo=TZ))
        else:
            run()
    except (RuntimeError, ValueError, urllib.error.URLError, OSError, zipfile.BadZipFile) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)

