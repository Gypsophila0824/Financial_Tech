"""黑箱重現：用輸入包重跑當天決策，驗證決策與委託和原 D-Plan 逐筆相同。

  .venv/Scripts/python.exe -X utf8 -m src.agent.reproduce output/live/inputs_<team_id>_<日期>.json
完整步驟見 docs/重現說明.md（解壓 output/code_archive/ 對應版本 → 建環境 → 執行本程式）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from . import codeversion
from .bundle import BundleInputs, load
from .reasoner import TemplateReasoner
from .run_daily import run_day
from .state import PortfolioState


def reproduce(bundle_path: Path) -> bool:
    data = load(bundle_path)
    current = codeversion.current()
    if current != data["code_version"]:
        print(f"注意：目前程式版本 {current} 與輸入包記錄的 {data['code_version']} 不同，"
              f"請先解壓 output/code_archive/ 中對應版本再執行。")
    result = run_day(data["trade_date"], mode="mock", team_id="MOCK", inputs=BundleInputs(data),
                     state=PortfolioState(**data["state"]), reasoner=TemplateReasoner(),
                     out_dir=Path(tempfile.mkdtemp(prefix="reproduce_")))
    got = {"decisions": [{k: d[k] for k in ("ticker", "action", "target_weight")} for d in result.plan["decisions"]],
           "no_trade": sorted(n["ticker"] for n in result.plan["no_trade_decisions"]),
           "orders": [{k: o[k] for k in ("ticker", "side", "shares")} for o in result.plan["orders"]]}
    ok = True
    for part in ("decisions", "no_trade", "orders"):
        same = got[part] == data["expected"][part]
        ok &= same
        print(f"{part}：{'相同' if same else '不同'}（{len(data['expected'][part])} 筆）")
        if not same:
            print(f"  原本：{data['expected'][part]}\n  重跑：{got[part]}")
    print("重現成功：決策與委託逐筆相同" if ok else "重現失敗")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法：python -m src.agent.reproduce <inputs_*.json>")
    sys.exit(0 if reproduce(Path(sys.argv[1])) else 1)
