"""
从 OCR 结构化 JSON 生成基金交易流水 Excel。

用法:
  python scripts/build_ledger.py                                    # 从 parsed_records/ 读取
  python scripts/build_ledger.py --input /path/to/jsons             # 自定义输入目录
  python scripts/build_ledger.py --fund-map fund_map.xlsx           # 指定基金映射表
  python scripts/build_ledger.py --fetch-nav                        # 启用 akshare 净值查询
"""

import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("需要 pandas: pip install pandas openpyxl")
    sys.exit(1)

# ── 状态标准化 ──────────────────────────────────────────────
STATUS_MAP = [
    (("成功", "已确认", "确认成功", "已完成"), "成功"),
    (("失败",), "失败"),
    (("撤回", "撤销", "已撤销", "取消", "已取消", "撤单"), "已撤销"),
    (("处理中", "确认中", "受理"), "处理中"),
]


def std_status(raw: str) -> str:
    if not raw:
        return "未知"
    for keywords, standard in STATUS_MAP:
        for kw in keywords:
            if kw in raw:
                return standard
    return "未知"


# ── action / direction ─────────────────────────────────────
ACTION_DIR = {
    "buy": ("buy", "买入"),
    "申购": ("buy", "买入"),
    "买入": ("buy", "买入"),
    "sell": ("sell", "卖出"),
    "赎回": ("sell", "卖出"),
    "卖出": ("sell", "卖出"),
    "dividend": ("dividend", "分红"),
    "分红": ("dividend", "分红"),
    "dividend_reinvest": ("dividend_reinvest", "红利再投资"),
    "红利再投": ("dividend_reinvest", "红利再投资"),
    "红利再投资": ("dividend_reinvest", "红利再投资"),
}


def std_action(raw: str) -> tuple[str, str]:
    if not raw:
        return ("", "")
    raw_lower = raw.lower().strip()
    if raw_lower in ACTION_DIR:
        return ACTION_DIR[raw_lower]
    for k, (a, d) in ACTION_DIR.items():
        if k in raw:
            return (a, d)
    return (raw, raw)


# ── 数值清洗 ───────────────────────────────────────────────
def clean_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("￥", "").replace("¥", "").replace("元", "").strip()
    if s in ("", "--", "-", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── 基金名称清洗 ────────────────────────────────────────────
def clean_fund_name(name: str) -> str:
    if not name:
        return ""
    return re.sub(r"\s+", "", name.replace("\n", "").replace("\r", "")).strip()


# ── 基金匹配 ────────────────────────────────────────────────
def load_fund_map(path: str | Path) -> list[dict]:
    p = Path(path)
    if p.exists():
        try:
            df = pd.read_excel(str(p), dtype=str)
            return df.fillna("").to_dict("records")
        except Exception as e:
            print(f"  fund_map 读取失败: {e}")
    else:
        print("  未找到 fund_map.xlsx，跳过基金代码匹配")
    return []


def match_fund(name: str, fund_map: list[dict]) -> dict:
    if not name or not fund_map:
        return {}
    for entry in fund_map:
        kw = entry.get("keyword", "")
        if kw and kw in name:
            return {
                "fund_name": entry.get("fund_name", name),
                "fund_code": entry.get("fund_code", ""),
                "fund_type": entry.get("fund_type", "unknown"),
            }
    return {}


# ── 读取输入 ────────────────────────────────────────────────
def load_records(input_dir: str) -> list[dict]:
    records = []
    for fname in sorted(os.listdir(input_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(input_dir, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            print(f"  {fname}: 读取失败 - {e}")
            continue
        if isinstance(data, list):
            for r in data:
                r["_source_file"] = fname
                records.append(r)
            print(f"  {fname}: {len(data)} 条")
        elif isinstance(data, dict):
            data["_source_file"] = fname
            records.append(data)
            print(f"  {fname}: 1 条")
    return records


# ── akshare 净值查询（可选）─────────────────────────────────
def fetch_nav_for_fund(fund_code: str, fund_type: str, trade_dates: list[date]) -> dict:
    """返回 {trade_date_str: nav_value} 的映射"""
    try:
        import akshare as ak
    except ImportError:
        return {}

    result = {}
    if not trade_dates:
        return result

    start = min(trade_dates).isoformat()
    end = max(trade_dates).isoformat()

    try:
        if fund_type == "etf":
            df = ak.fund_etf_hist_em(symbol=fund_code, period="daily", start_date=start, end_date=end, adjust="")
            if "收盘" in df.columns:
                for _, row in df.iterrows():
                    try:
                        d = pd.to_datetime(row["日期"]).date()
                        result[d.isoformat()] = float(row["收盘"])
                    except Exception:
                        pass
        else:
            df = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
            if "单位净值" in df.columns and "净值日期" in df.columns:
                for _, row in df.iterrows():
                    try:
                        d = pd.to_datetime(row["净值日期"]).date()
                        result[d.isoformat()] = float(row["单位净值"])
                    except Exception:
                        pass
    except Exception as e:
        print(f"    akshare 查询 {fund_code} 失败: {e}")

    return result


def find_nearest_nav(trade_date: date, nav_map: dict[str, float]) -> tuple[str, float] | None:
    ds = trade_date.isoformat()
    if ds in nav_map:
        return (ds, nav_map[ds])
    dates = []
    for k in nav_map:
        try:
            dates.append((datetime.strptime(k, "%Y-%m-%d").date(), k))
        except Exception:
            pass
    dates.sort(key=lambda x: x[0])
    # 找之后最近
    for d, k in dates:
        if d >= trade_date:
            return (k, nav_map[k])
    # 找之前最近
    for d, k in reversed(dates):
        if d < trade_date:
            return (k, nav_map[k])
    return None


# ── 估算逻辑 ────────────────────────────────────────────────
def estimate(row: dict) -> dict:
    r = dict(row)
    action = r.get("action", "")
    amt = r.get("amount_cny")
    sh = r.get("share")
    nav = r.get("nav")

    if action == "buy":
        r["cash_flow"] = -amt if amt is not None else None
        if sh is None and amt is not None and nav:
            r["estimated_share"] = round(amt / nav, 2)
        r["share_flow"] = sh if sh is not None else r.get("estimated_share")

    elif action == "sell":
        r["share_flow"] = -sh if sh is not None else None
        if amt is None and sh is not None and nav:
            r["estimated_amount_cny"] = round(sh * nav, 2)
        r["cash_flow"] = amt if amt is not None else r.get("estimated_amount_cny")

    elif action in ("dividend", "dividend_reinvest"):
        r["cash_flow"] = amt
        r["share_flow"] = sh
        r["note"] = (r.get("note", "") + "；" if r.get("note") else "") + "分红/红利再投资需人工确认"

    else:
        r["cash_flow"] = amt
        r["share_flow"] = sh

    return r


# ── 主流程 ──────────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="从 OCR 结构化 JSON 生成基金流水 Excel")
    parser.add_argument("--input", default="", help="输入目录（默认: parsed_records/）")
    parser.add_argument("--fund-map", default="", help="基金映射表 fund_map.xlsx 路径")
    parser.add_argument("--output", default="", help="输出路径（默认: output/基金流水_整理版.xlsx）")
    parser.add_argument("--fetch-nav", action="store_true", help="启用 akshare 净值查询")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    input_dir = args.input or str(project_root / "parsed_records")
    output_dir = project_root / "output"
    output_path = args.output or str(output_dir / "基金流水_整理版.xlsx")
    fund_map_path = args.fund_map or str(project_root / "fund_map.xlsx")

    # 如果 parsed_records 不存在，回退到 uploads
    if not os.path.isdir(input_dir):
        fallback = "/www/data/fund-ledger/uploads"
        if os.path.isdir(fallback):
            input_dir = fallback
            print(f"parsed_records/ 不存在，使用 {fallback}")
        else:
            print(f"输入目录 {input_dir} 不存在")
            return

    os.makedirs(str(output_dir), exist_ok=True)

    # ── 1. 读取 ──
    print("读取 OCR 结构化数据...")
    records = load_records(input_dir)
    if not records:
        print("没有找到任何记录")
        return
    print(f"共 {len(records)} 条记录")

    # ── 2. 清洗 ──
    print("清洗数据...")
    fund_map = load_fund_map(fund_map_path)

    rows = []
    for r in records:
        fn_raw = clean_fund_name(r.get("fund_name", ""))
        a_std, a_dir = std_action(r.get("action", ""))
        s_std = std_status(r.get("status", ""))
        matched = match_fund(fn_raw, fund_map)
        td = r.get("trade_date", "")
        sa = r.get("submitted_at", "")

        note_parts = []
        if not matched:
            note_parts.append("未匹配基金")

        row = {
            "source_file": r.get("_source_file", ""),
            "fund_name_raw": fn_raw,
            "fund_name": matched.get("fund_name", fn_raw) if matched else fn_raw,
            "fund_code": matched.get("fund_code", "") if matched else "",
            "fund_type": matched.get("fund_type", "") if matched else "",
            "action": a_std,
            "direction": a_dir,
            "trade_date": td,
            "submitted_at": sa,
            "share": clean_num(r.get("share")),
            "amount_cny": clean_num(r.get("amount_cny")),
            "status": r.get("status", ""),
            "status_std": s_std,
            "nav": None,
            "nav_date": None,
            "estimated_share": None,
            "estimated_amount_cny": None,
            "cash_flow": None,
            "share_flow": None,
            "note": "；".join(note_parts) if note_parts else "",
        }
        rows.append(row)

    df = pd.DataFrame(rows)

    # ── 3. 净值查询（可选）──
    if args.fetch_nav:
        print("查询净值...")
        success_df = df[df["status_std"] == "成功"].copy()
        for fund_code in success_df["fund_code"].unique():
            if not fund_code:
                continue
            sub = success_df[success_df["fund_code"] == fund_code]
            fund_type = sub.iloc[0].get("fund_type", "open_fund")
            trade_dates = []
            for d in sub["trade_date"].dropna():
                try:
                    trade_dates.append(pd.to_datetime(d).date())
                except Exception:
                    pass
            if not trade_dates:
                continue
            print(f"  {fund_code} ({sub.iloc[0]['fund_name'][:20]}...) - {len(trade_dates)} 条")
            nav_map = fetch_nav_for_fund(fund_code, fund_type, trade_dates)
            if not nav_map:
                continue
            for idx in sub.index:
                td = sub.at[idx, "trade_date"]
                try:
                    trade_date = pd.to_datetime(td).date()
                except Exception:
                    continue
                nearest = find_nearest_nav(trade_date, nav_map)
                if nearest:
                    nav_date_str, nav_val = nearest
                    df.at[idx, "nav"] = nav_val
                    df.at[idx, "nav_date"] = nav_date_str
                else:
                    note = df.at[idx, "note"] or ""
                    note = (note + "；" if note else "") + "净值缺失"
                    df.at[idx, "note"] = note
        print("  净值查询完成")

    # ── 4. 估算 ──
    print("计算现金/份额流...")
    estimated_rows = []
    for _, r in df.iterrows():
        estimated_rows.append(estimate(r.to_dict()))
    df = pd.DataFrame(estimated_rows)

    # ── 5. 日期格式化 ──
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce", format="mixed").dt.date
    if "submitted_at" in df.columns:
        df["submitted_at"] = pd.to_datetime(df["submitted_at"], errors="coerce")

    # ── 6. 导出 ──
    all_success = df[df["status_std"] == "成功"].copy()
    unmatched = df[(df["fund_code"] == "") & (df["fund_name_raw"] != "")].copy()
    missing_nav = df[(df["status_std"] == "成功") & (df["nav"].isna())].copy()

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="全部流水", index=False)
        all_success.to_excel(writer, sheet_name="成功流水", index=False)
        unmatched.to_excel(writer, sheet_name="未匹配基金", index=False)
        missing_nav.to_excel(writer, sheet_name="净值缺失记录", index=False)

    print(f"\n✅ 已生成: {output_path}")
    print(f"   全部流水: {len(df)} 条")
    print(f"   成功流水: {len(all_success)} 条")
    print(f"   未匹配基金: {len(unmatched)} 条")
    print(f"   净值缺失: {len(missing_nav)} 条")


if __name__ == "__main__":
    main()
