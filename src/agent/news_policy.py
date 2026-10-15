"""正式與研究共用的新聞否決；A3 必須由正式還原價格因子提供。"""
import pandas as pd

from . import strategy as S


def news_veto(worst_event: pd.Series, a3: pd.Series) -> pd.Series:
    # 保持正式策略原有行為：嚴格小於，A3 缺值不自行推論為跌破。
    return worst_event.fillna("").str.startswith(f"{S.VETO_MATERIALITY}:") & (a3 < S.VETO_TREND_MAX)
