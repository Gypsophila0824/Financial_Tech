"""Regression evidence from TPEx and competition screenshots on 2026-10-22."""

import copy
import csv
import sys
from datetime import date

import pandas as pd
import pytest

from src.agent.datafeed import market, tpex_prices as prices
from src.agent.inputs import StoreInputs


DAY = '2026-10-22'
FACTS = [
    ('1815', '富喬', 150488117, 22123561712, 139, 147.01),
    ('3081', '聯亞', 3613716, 8952729835, 2375, 2477.43),
    ('4991', '環宇-KY', 2945535, 1269749215, 415, 431.08),
    ('6187', '萬潤', 2038480, 2672874685, 1260, 1311.05),
    ('6274', '台燿', 18732500, 28439419230, 1420, 1518.19),
    ('8299', '群聯', 1897872, 3623984600, 1880, 1909.50),
]


def payloads():
    daily = {'stat': 'ok', 'date': '20261022', 'tables': [{
        'fields': ['代號', '名稱', '開盤', '最高', '最低', '收盤', '成交股數', '成交金額(元)', '次日 參考價'],
        'data': [[t, n, str(c), str(c), str(c), str(c), f'{v:,}', f'{a:,}', str(c)] for t, n, v, a, c, _ in FACTS],
        'totalCount': len(FACTS)}]}
    block = {'stat': 'ok', 'date': '20260101', 'tables': [{
        'fields': ['證券代號', '名 稱', '成交日期', '成交股數', '成交金額'],
        'data': [['6187', '萬潤 ', 1151022, '15,000', '19,987,500'],
                 ['6187', '萬潤 ', 1150924, '24,000', '28,559,280']], 'totalCount': 2}]}
    return daily, block


def getter(daily, block, calls=None):
    def get(url, pause):
        if calls is not None:
            calls.append(url)
        return copy.deepcopy(block if '/blockTrade/' in url else daily)
    return get


def test_six_platform_prices_and_raw_evidence():
    daily, block = payloads()
    result = prices.fetch_quotes('2026/10/22', getter(daily, block), 0)
    for ticker, _, volume, value, close, expected in FACTS:
        o, h, low, c, v, a, ref = result[ticker]
        assert round(a / v, 2) == expected
        assert c == ref == close
        audit = result.audit[ticker]
        assert audit['total_volume'] == volume and audit['total_value'] == value
        assert audit['price_scope'] == prices.PRICE_SCOPE
        assert audit['block_volume'] == (15000 if ticker == '6187' else 0)
    assert result['6187'][4:6] == (2023480, 2652887185)
    assert round(result['6187'][5] / result['6187'][4], 4) == 1311.0518


def test_multiple_block_rows_sum_only_the_requested_day():
    daily, block = payloads()
    block['tables'][0]['data'][0] = ['6187', '萬潤', 1151022, '5,000', '6,662,500']
    block['tables'][0]['data'].append(['6187', '萬潤', '115/10/22', '10,000', '13,325,000'])
    block['tables'][0]['totalCount'] = 3
    result = prices.fetch_quotes('2026/10/22', getter(daily, block), 0)
    assert result['6187'][4:6] == (2023480, 2652887185)


def test_yearly_blocks_are_reused_without_refetching_daily_quotes():
    daily, block = payloads()
    calls = []
    get = getter(daily, block, calls)
    prices.fetch_quotes('2026/10/22', get, 0)
    prices.fetch_quotes('2026/10/22', get, 0)
    assert sum('/blockTrade/' in url for url in calls) == 1
    assert sum('/dailyQuotes?' in url for url in calls) == 2


@pytest.mark.parametrize('bad', [
    {'stat': 'error', 'date': '20260101', 'tables': []},
    {'stat': 'ok', 'date': '20260101', 'tables': []},
    {'stat': 'ok', 'date': '20250101', 'tables': []},
])
def test_failed_or_malformed_block_query_is_not_zero(bad):
    daily, _ = payloads()
    with pytest.raises(ValueError):
        prices.fetch_quotes('2026/10/22', getter(daily, bad), 0)


def test_verified_empty_block_table_is_allowed():
    daily, block = payloads()
    block['tables'][0].update(data=[], totalCount=0)
    result = prices.fetch_quotes('2026/10/22', getter(daily, block), 0)
    assert result['6187'][4:6] == (2038480, 2672874685)
    assert result.audit['6187']['block_volume'] == 0


@pytest.mark.parametrize('target', ['daily', 'block'])
def test_partial_api_response_stops(target):
    daily, block = payloads()
    (daily if target == 'daily' else block)['tables'][0]['totalCount'] += 1
    with pytest.raises(ValueError, match='不完整'):
        prices.fetch_quotes('2026/10/22', getter(daily, block), 0)


def test_wrong_daily_date_stops():
    daily, block = payloads()
    daily['date'] = '20261021'
    with pytest.raises(ValueError, match='日期'):
        prices.fetch_quotes('2026/10/22', getter(daily, block), 0)


def test_subtraction_cannot_produce_negative_volume():
    daily, block = payloads()
    block['tables'][0]['data'][0][3] = '9,000,000'
    with pytest.raises(ValueError, match='扣除鉅額'):
        prices.fetch_quotes('2026/10/22', getter(daily, block), 0)


def test_dates_accept_numeric_roc_and_gregorian_forms():
    assert prices.parse_date(1151022) == prices.parse_date('115/10/22') == prices.parse_date('20261022') == date(2026, 10, 22)


def test_old_cache_and_fallback_need_refresh():
    for source in ('tpex', 'tpex_openapi', 'mis', 'finmind'):
        assert prices.needs_refresh({'market': 'TPEX', 'src': source})
    assert not prices.needs_refresh({'market': 'TPEX', 'price_scope': prices.PRICE_SCOPE})
    assert not prices.needs_refresh({'market': 'TWSE'})


@pytest.mark.parametrize('scope', [None, '', 'old', 'tpex_openapi'])
def test_store_rejects_old_or_fallback_otc_average(scope):
    inputs = StoreInputs.__new__(StoreInputs)
    inputs._daily = pd.DataFrame([dict(date=DAY, ticker='6187', market='TPEX', avg_price=1311.6502, price_scope=scope)])
    with pytest.raises(ValueError, match='上櫃均價'):
        inputs.fill_prices(DAY)


def test_fetch_day_saves_audit_and_store_uses_corrected_average(monkeypatch):
    daily, block = payloads()
    monkeypatch.setattr(market, 'get_json', getter(daily, block))
    monkeypatch.setattr(market, 'twse_quotes', lambda _: {})
    monkeypatch.setattr(market, 'twse_insti', lambda _: {})
    monkeypatch.setattr(market, 'tpex_insti', lambda _: {})
    universe = [(t, 'TPEX') for t, *_ in FACTS]
    rows = market.fetch_day(date(2026, 10, 22), universe)
    inputs = StoreInputs.__new__(StoreInputs)
    inputs._daily = pd.DataFrame(rows)
    fills = inputs.fill_prices(DAY)
    assert fills['6187'] == 1311.0518
    for row in rows:
        assert row['price_scope'] == prices.PRICE_SCOPE
        assert set(prices.AUDIT_FIELDS) <= row.keys()
    assert next(row for row in rows if row['ticker'] == '6187')['block_value'] == 19987500


@pytest.mark.parametrize('success', [True, False])
def test_targeted_refresh_replaces_only_requested_day(monkeypatch, tmp_path, success):
    master = tmp_path/'security_master.csv'
    master.write_text('ticker,market\n6187,TPEX\n', encoding='utf-8')
    path = tmp_path/'daily.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=market.FIELDS)
        writer.writeheader()
        writer.writerows([dict(date='2026-10-21', ticker='6187', market='TPEX', value=123),
                          dict(date=DAY, ticker='6187', market='TPEX', value=456)])
    before = path.read_bytes()
    monkeypatch.setattr(market, 'SECURITY_MASTER', master)
    monkeypatch.setattr(market, 'OUT_PATH', path)
    monkeypatch.setattr(sys, 'argv', ['market', '--date', DAY])
    calls = []
    def fetch(day, universe, allow_fallback):
        calls.append((day, universe, allow_fallback))
        return [dict(date=DAY, ticker='6187', market='TPEX', value=2652887185,
                     volume=2023480, avg_price=1311.0518, price_scope=prices.PRICE_SCOPE)] if success else None
    monkeypatch.setattr(market, 'fetch_day', fetch)
    if not success:
        with pytest.raises(SystemExit, match='未更新'):
            market.main()
        assert path.read_bytes() == before
    else:
        market.main()
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 2
        assert rows[0]['date'] == '2026-10-21' and rows[0]['value'] == '123'
        assert rows[1]['avg_price'] == '1311.0518'
        assert rows[1]['price_scope'] == prices.PRICE_SCOPE
    assert calls == [(date(2026, 10, 22), [('6187', 'TPEX')], False)]
