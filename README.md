# Fund Ledger

Fund Ledger is a self-hosted fund bookkeeping app for domestic China mutual fund screenshots.

The current main workflow is:

```text
upload fund company screenshots
-> Baidu table OCR
-> parse every OCR table row as one operation
-> auto-match fund code and rules
-> auto-calculate NAV date, shares, redemption fee, amount
-> auto-post high-quality rows
-> keep uncertain rows for manual review
```

It is built for the practical case where fund company websites show transaction tables but do not provide complete statement exports.

## Current Scope

- Upload screenshots in bulk.
- Run Baidu table OCR only.
- Preserve every OCR row.
- Parse each table row as an independent operation.
- Auto-post high-quality transactions.
- Keep cancelled, failed, setup, and other non-money operations as events.
- Show transactions, events, holdings, fund rules, NAV charts, and buy/sell markers.
- Support local alias mapping for OCR-noisy fund names.

旧交易数据不迁移。当前版本以截图流水账本为主线。

## Data Privacy

The repository should contain code only.

Do not commit:

- `.env`
- SQLite databases
- uploaded screenshots
- OCR output files
- `fund_map.xlsx`
- personal fund mappings or account-tail data
- API keys

`fund_map.xlsx` is intentionally ignored by Git. It can exist locally at the project root and will be used as a private alias seed file.

## Paths

| Purpose | Path |
|---|---|
| Project | `/www/projects/fund-ledger` |
| Data directory | `/www/data/fund-ledger` |
| SQLite database | `/www/data/fund-ledger/fund-ledger.sqlite3` |
| Uploads | `/www/data/fund-ledger/uploads` |
| Local private alias seed | `/www/projects/fund-ledger/fund_map.xlsx` |

## Quick Start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example /etc/fund-ledger.env
uvicorn app.main:app --host 127.0.0.1 --port 4330
```

For production, run it behind systemd and nginx.

## Configuration

Required runtime settings:

```bash
APP_SECRET_KEY=change-me
FUND_LEDGER_USERNAME=admin
FUND_LEDGER_PASSWORD_HASH=...
FUND_LEDGER_DATA_DIR=/www/data/fund-ledger
FUND_LEDGER_DB=/www/data/fund-ledger/fund-ledger.sqlite3
OCR_BACKEND=baidu_table
BAIDU_OCR_API_KEY=
BAIDU_OCR_SECRET_KEY=
BAIDU_TABLE_OCR_ENDPOINT=https://aip.baidubce.com/rest/2.0/ocr/v1/table
```

The `/settings` page can update Baidu OCR credentials at runtime. Secret values are masked in the UI.

## OCR

Only Baidu table OCR is part of the active import chain.

RapidOCR/PaddleOCR/generic OCR fallback are intentionally not used in the current workflow. This keeps the table structure stable enough for transaction parsing.

## Import Rules

Each OCR table row is treated as one operation.

The system does not deduplicate rows by same fund, same time, or same amount. It only prevents re-importing the exact same OCR row from the same source document.

Status handling:

- `成功 / 已确认 / 已完成`: eligible for auto-posting.
- `撤销 / 失败 / 取消`: retained as events, not posted as transactions.
- unknown status: kept for manual review.

Supported operation handling:

- `申购 / 认购 / 买入 / 定投`: buy.
- `赎回 / 卖出 / 转换 / 转换出`: sell.
- `现金分红`: dividend.
- `红利再投资`: dividend reinvestment.
- `修改分红方式`: event, updates dividend preference when posted.
- `开始定投 / 停止定投 / 修改定投`: event, does not affect holdings.
- `强制调增 / 强制调减`: event.

## Automatic Calculations

For open-end funds:

- Use Asia/Shanghai wall time for OCR transaction timestamps.
- 15:00 cutoff:
  - before 15:00 uses that trade date.
  - at or after 15:00 uses the next available NAV date.
- Historical NAV is synced from open data providers.
- T+N confirmation date is inferred from fund rules.
- Buy shares are calculated from amount, fee, and NAV.
- Sell gross amount is `share * NAV`.
- Redemption fee is estimated with FIFO lots and redemption-fee tiers.
- Sell amount is shown as net amount after redemption fee.

For ETF and money-fund rows:

- Rows are kept as ledger/events.
- They are excluded from default profit and NAV-curve summaries.

## Fund Matching

Fund name matching uses:

1. local `FundAlias` rows;
2. private local `fund_map.xlsx` seed file, when present;
3. open fund-name search;
4. manual correction.

OCR-noisy names are normalized before matching:

- remove accidental spaces;
- normalize full-width/half-width parentheses;
- tolerate common spacing noise such as `人民 币份额`;
- compare aliases with punctuation removed.

For `fund_map.xlsx`, the keyword may itself be an OCR-noisy alias. It is used to find the fund code and type. It is not treated as a public canonical name.

## Pages

| Page | Purpose |
|---|---|
| `/upload` | Upload screenshots or pasted text |
| `/imports` | Import document list and bulk OCR |
| `/imports/{id}` | OCR rows, candidates, file-level auto-post |
| `/candidates` | Manual review and global auto-post |
| `/transactions` | Posted transactions, grouped by collapsible fund sections |
| `/events` | Non-money and ignored-status events |
| `/holdings` | Shares, cost, market value, profit |
| `/funds` | Aliases and fund rules |
| `/funds/{code}` | Fund detail and NAV curve with buy/sell markers |
| `/settings` | OCR settings |

## Systemd

```bash
systemctl status fund-ledger.service
systemctl restart fund-ledger.service
journalctl -u fund-ledger.service --since "10 min ago"
```

## Tests

```bash
.venv/bin/python -m pytest
```

The test suite covers:

- OCR row parsing;
- status and action classification;
- duplicate-row independence;
- fund-name alias normalization;
- auto NAV sync;
- 15:00 cutoff;
- T+N confirmation dates;
- sell fee estimation;
- unposted-candidate FIFO;
- ETF and money-fund exclusion from holdings;
- import-level and OCR-level auto-posting;
- page rendering.

## Roadmap

Next major phase: import complete Fund E Account exports as a reconciliation source.

Planned reconciliation checks:

- transaction date and confirmation date;
- fund code and fund type;
- share amount;
- NAV date and NAV value;
- redemption fee;
- net sell amount;
- dividend and dividend reinvestment behavior.

If calculated results do not match the Fund E Account data, the app should flag the row and explain likely causes, such as wrong fund code, wrong NAV date, missing fee tier, OCR status error, or misclassified conversion/dividend operation.
