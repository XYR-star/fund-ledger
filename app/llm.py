import json
import os
from dataclasses import dataclass

import requests


@dataclass
class LlmParseResult:
    raw_response: str
    parsed_json: list[dict] | None


def is_deepseek_configured(config: dict[str, str] | None = None) -> bool:
    return _enabled(config, "DEEPSEEK_ENABLED") and bool(_value(config, "DEEPSEEK_API_KEY"))


def parse_with_deepseek(raw_text: str, config: dict[str, str] | None = None) -> LlmParseResult | None:
    api_key = _value(config, "DEEPSEEK_API_KEY")
    if not api_key or not _enabled(config, "DEEPSEEK_ENABLED"):
        return None

    base_url = _value(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = _value(config, "DEEPSEEK_MODEL", "deepseek-chat")
    prompt = (
        "请从下面的中国场外基金交易记录文本中提取交易。"
        "不同基金销售平台（如易方达、天天基金、支付宝、银行等）的导出格式不同。"
        "请根据文本内容判断：买入记录中的数值通常是申购金额(amount_cny)，份额(share)需根据金额和手续费推算；"
        "卖出记录中的数值通常是赎回份额(share)，金额(amount_cny)需根据份额和净值推算。"
        "只提取状态为成功、已确认、确认成功的有效交易；状态为已撤销、撤单、已取消、失败、作废、交易关闭的记录不要作为有效交易。"
        "如果你仍然保留这类无效记录，必须把 transaction_status 写成原文状态，并把 status 写成 ignored。"
        "如果能识别出导出平台来源，请在每行记录中标注。"
        "只返回 JSON 数组。字段为 fund_code, fund_name, trade_date, submitted_at, confirm_date, "
        "action, amount_cny, share, nav, fee, transaction_status, status, source_platform。action 只能是 buy, sell, dividend, "
        "dividend_reinvest, fee_adjustment。submitted_at 为提交时间，格式 HH:MM；"
        "如果文本里没有明确时间则用 null。无法确定的字段用 null。\n\n"
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


def resolve_fund_code_by_name(fund_name: str, config: dict[str, str] | None = None) -> str | None:
    api_key = _value(config, "DEEPSEEK_API_KEY")
    if not api_key or not _enabled(config, "DEEPSEEK_ENABLED"):
        return None

    base_url = _value(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = _value(config, "DEEPSEEK_MODEL", "deepseek-chat")
    prompt = (
        f"请判断以下中国公募基金的名称\"{fund_name}\"对应的6位数字基金代码。"
        "只返回6位数字，不要任何解释。如果无法确定，返回 unknown。"
    )
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=15,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.isdigit() and len(content) == 6:
            return content
    except Exception:
        pass
    return None


def _value(config: dict[str, str] | None, key: str, default: str = "") -> str:
    if config is not None:
        return config.get(key, default)
    return os.getenv(key, default)


def _enabled(config: dict[str, str] | None, key: str) -> bool:
    return _value(config, key, "true").lower() in {"1", "true", "yes", "on"}


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
