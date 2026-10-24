import pandas as pd
import pytest

from src.agent.datafeed.etf_holdings import is_stock
from src.agent.factor_engine import etf_top_holdings


@pytest.mark.parametrize('kind', ['C', 'P'])
def test_moneydj_english_index_option_is_not_a_stock(kind):
    name = f'TAIWAN TAIE {kind} 18NOV26 46500 TWSE'
    assert not is_stock(name, '', '')
    rows = [dict(etf_id='00401A', name=name, code='', market='',
                 weight_pct=-.63, is_stock=is_stock(name, '', '')),
            dict(etf_id='00401A', name='台積電', code='2330', market='TW',
                 weight_pct=10, is_stock=is_stock('台積電', '2330', 'TW'))]
    assert etf_top_holdings(pd.DataFrame(rows))['00401A'].to_dict() == {'2330': .1}


def test_overseas_stock_and_invalid_stock_weight_keep_existing_checks():
    assert is_stock('Netflix', 'NFLX', 'US')
    assert is_stock('TAIWAN SEMICONDUCTOR', 'TSM', 'US')
    with pytest.raises(ValueError, match='個股權重'):
        etf_top_holdings(pd.DataFrame([dict(etf_id='TEST', name='Netflix',
            code='NFLX', market='US', weight_pct=-.63, is_stock=True)]))
