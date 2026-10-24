from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.agent.inputs import StoreInputs
from src.agent.team_research_strategy import TeamResearchStrategy
from src.agent.backtest.runner import run_formal_backtest,write_result
out=Path('output/validation/backtest/cap_tier_search_20261023')
out.mkdir(parents=True,exist_ok=True)
inputs=StoreInputs(history=True,strict_history=True)
adapter=TeamResearchStrategy(pv_weight=.4,fund_weight=.5,news_weight=.1,buffer_rank=50,no_trade_band=.03)
result=run_formal_backtest(inputs,start='2026-10-01',end='2026-10-22',strategy_adapter=adapter,price_only=True)
write_result(result,out/'candidate_baseline',config={'purpose':'candidate baseline for cap-tier search','price_only':True})
print(json.dumps({k:result.summary[k] for k in ['records','start_date','end_date','cumulative_return','max_drawdown','total_cost']},ensure_ascii=False))
