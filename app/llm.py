import json
import os
from dataclasses import dataclass

import requests


@dataclass
class LlmParseResult:
    raw_response: str
    parsed_json: list[dict] | None


def is_deepseek_configured(config: dict[str, str] | None = None) -> bool:
    return bool(_value(config, "DEEPSEEK_API_KEY"))


def parse_with_deepseek(raw_text: str, config: dict[str, str] | None = None) -> LlmParseResult | None:
    api_key = _value(config, "DEEPSEEK_API_KEY")
    if not api_key:
        return None

    base_url = _value(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = _value(config, "DEEPSEEK_MODEL", "deepseek-chat")
    prompt = (
        "请从下面的中国场外基金交易记录文本中提取交易，"
        "只返回 JSON 数组。字段为 fund_code, fund_name, trade_date, confirm_date, "
        "action, amount_cny, share, nav, fee。action 只能是 buy, sell, dividend, "
        "dividend_reinvest, fee_adjustment。无法确定的字段用 null。\n\n"
        f"{raw_text}"
    )
    response = requests.post(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return LlmParseResult(raw_response=content, parsed_json=_extract_json_array(content))


def _value(config: dict[str, str] | None, key: str, default: str = "") -> str:
    if config is not None:
        return config.get(key, default)
    return os.getenv(key, default)


def _extract_json_array(text: str) -> list[dict] | None:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None
