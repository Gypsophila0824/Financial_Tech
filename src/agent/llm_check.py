"""LLM 連線檢查：發一個極小的請求，確認 .env 設定與金鑰有效。

執行：.venv/Scripts/python.exe -X utf8 -m src.agent.llm_check
"""

from __future__ import annotations

import sys

from .llm import AnthropicLLM, LLMError

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def main() -> None:
    try:
        llm = AnthropicLLM(effort="low")
        print(f"設定：provider={llm.provider}，model={llm.model}")
        result = llm.json("你是測試助手。", "請用一句繁體中文回答：連線測試成功了嗎？", SCHEMA, max_tokens=2000)
    except LLMError as exc:
        sys.exit(f"失敗：{exc}")
    except Exception as exc:  # 例如尚未設定金鑰時 SDK 在建立用戶端就會報錯
        sys.exit(f"失敗：{type(exc).__name__}：{exc}")
    print(f"成功：{result['answer']}")
    print(f"實際回應模型：{llm.model_version}；輸入 {llm.input_tokens} tokens、輸出 {llm.output_tokens} tokens")


if __name__ == "__main__":
    main()
