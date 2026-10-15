"""基本面：最新月營收表未列入的股票，沿用較早存檔中已公布的一期（與回測 fund_history 口徑一致）。"""

from __future__ import annotations

import json

from src.agent.datafeed import fund_factors as ff


def rev(code, month, yoy):
    return {"公司代號": code, "資料年月": month, "營業收入-去年同月增減(%)": str(yoy), "累計營業收入-前期比較增減(%)": "10"}


def write(d, twse_rev):
    d.mkdir(parents=True)
    files = {"twse_revenue": twse_rev, "tpex_revenue": [],
             "twse_margin": [{"公司代號": "2330", "年度": "115", "季別": "2", "營業利益率(%)(營業利益)/(營業收入)": "50"}],
             "tpex_margin": []}
    for name, rows in files.items():
        (d / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def test_revenue_falls_back_to_previous_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "RAW_DIR", tmp_path)
    write(tmp_path / "2026-10-04", [rev("2330", "11508", 30), rev("2308", "11508", 35)])
    write(tmp_path / "2026-10-13", [rev("2330", "11509", 40)])          # 9 月表尚未列入 2308
    f = ff.compute(tmp_path / "2026-10-13")
    assert f.loc["2330", "rev_month"] == "2026-09" and f.loc["2330", "B1"] == 40
    assert f.loc["2308", "rev_month"] == "2026-08" and f.loc["2308", "B1"] == 35
    assert f.attrs["revenue_fallback"] == ["2308（2026-08，2026-10-04 存檔）"]


def test_no_lookahead_and_stale_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "RAW_DIR", tmp_path)
    write(tmp_path / "2026-08-01", [rev("2308", "11506", 99)])          # 超過 40 天，不沿用
    write(tmp_path / "2026-10-13", [rev("2330", "11509", 40)])
    write(tmp_path / "2026-10-20", [rev("2308", "11509", 77)])          # 未來的存檔，不得使用
    f = ff.compute(tmp_path / "2026-10-13")
    assert f["rev_month"].isna()["2308"] and f.attrs["revenue_fallback"] == []
