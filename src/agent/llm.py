"""LLM 用戶端：所有 LLM 呼叫都經過這裡，回傳經 JSON schema 約束的結果。

設定（環境變數或專案根目錄 .env，.env 不會被打包進 code_archive）：
  LLM_PROVIDER   anthropic（預設，直接呼叫 Anthropic API）或 vertex（Google Cloud Vertex AI 上的 Claude）
  LLM_MODEL      預設 claude-opus-5-5
  LLM_EFFORT     low / medium / high / xhigh / max，預設 medium
  ANTHROPIC_API_KEY                          provider=anthropic 時使用（或 `ant auth login` 的設定檔）
  GOOGLE_CLOUD_PROJECT、GOOGLE_CLOUD_REGION   provider=vertex 時使用（認證走 gcloud ADC）

合規：六(一) 投資決策須由 AI Agent 自主產生。LLM 失敗時直接拋錯，不得悄悄改用模板頂替；
實際回應的模型會記錄在 models_used，寫進 D-Plan 的 agent_metadata.model_version。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """整體無法使用（沒有金鑰、金鑰無效、權限不足）：其餘請求也必定失敗，應立即全部改用範本。"""


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """讀取 KEY=VALUE 格式的 .env；已存在的環境變數不覆蓋。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class AnthropicLLM:
    is_mock = False
    model_provider = "anthropic"

    def __init__(self, provider: str | None = None, model: str | None = None, effort: str | None = "env",
                 timeout: float | None = None):
        """effort=None 表示不送 effort（Haiku 4.5 不支援）；Haiku 4.5 另以 temperature 0 讓判讀穩定。"""
        load_dotenv()
        self.provider = provider or os.environ.get("LLM_PROVIDER", "anthropic")
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.effort = os.environ.get("LLM_EFFORT", "medium") if effort == "env" else effort
        self.timeout = timeout
        if self.provider == "anthropic":
            self.client = anthropic.Anthropic()
        elif self.provider == "vertex":
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise LLMError("provider=vertex 需要設定 GOOGLE_CLOUD_PROJECT")
            self.client = anthropic.AnthropicVertex(project_id=project,
                                                    region=os.environ.get("GOOGLE_CLOUD_REGION", "global"))
        else:
            raise LLMError(f"未知的 LLM_PROVIDER：{self.provider}")
        self.models_used: set[str] = set()
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def json(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}], output_config=output_config)
        if self.timeout:
            kwargs["timeout"] = self.timeout
        legacy = self.model.startswith("claude-haiku-4")
        if legacy:
            kwargs["extra_body"] = {"temperature": 0}     # Haiku 4.5 仍接受取樣參數（Haiku 5.5 起已停用 temperature）
        try:
            if self.provider == "anthropic" and not legacy:
                # 安全分類器拒答時由伺服器自動改用備援模型（Claude API 專屬；Haiku 4.5 不適用）
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailable(f"LLM 認證失敗：{exc.message}") from exc
        except TypeError as exc:      # 完全沒有認證資訊時，SDK 在送出請求前丟出 TypeError
            if "authentication" not in str(exc):
                raise
            raise LLMUnavailable("找不到 API 金鑰（請在 .env 設定 ANTHROPIC_API_KEY）") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"LLM API 錯誤 {exc.status_code}：{exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"無法連線到 LLM：{exc}") from exc

        self.calls += 1
        self.models_used.add(resp.model)
        self.input_tokens += resp.usage.input_tokens or 0
        self.output_tokens += resp.usage.output_tokens or 0
        if resp.stop_reason == "refusal":
            raise LLMError(f"LLM 拒絕回答：{getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise LLMError("LLM 輸出被 max_tokens 截斷")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("LLM 回應中沒有文字內容")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 回傳的不是合法 JSON：{text[:200]}") from exc

    @property
    def model_version(self) -> str:
        used = sorted(self.models_used) or [self.model]
        return "+".join(used)[:60]


class FakeLLM:
    """測試用：依呼叫順序回傳預先準備的 JSON，或由 handler(system, user, schema) 產生。"""

    is_mock = True
    model_provider = "other"
    model_version = "fake-llm"

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self.input_tokens = self.output_tokens = 0

    def json(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
        self.calls += 1
        return self.handler(system, user, schema)
