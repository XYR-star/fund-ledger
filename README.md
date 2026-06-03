# Fund Ledger

中国场外基金记账工具。支持从截图、文本、PDF 等来源导入交易记录，自动识别基金代码、计算份额/金额、推算确认日，管理持仓与收益曲线。

## 路径

| 用途 | 路径 |
|---|---|
| 项目代码 | `/www/projects/fund-ledger` |
| 数据目录 | `/www/data/fund-ledger` |
| SQLite 数据库 | `/www/data/fund-ledger/fund-ledger.sqlite3` |
| 上传文件 | `/www/data/fund-ledger/uploads` |
| NAV 缓存 | `/www/data/fund-ledger/cache/nav` |

## 快速启动

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example /etc/fund-ledger.env
uvicorn app.main:app --host 127.0.0.1 --port 4330
```

## 功能概览

### 导入流水线

1. **上传** — 支持截图（png/jpg）、PDF、纯文本。可一次选择多个文件批量导入。
2. **OCR 识别** — 默认使用 RapidOCR（本地、轻量），支持 PDF 逐页 OCR。可选 PaddleOCR 或 Baidu API。
3. **智能解析** — 
   - **结构化文本**：`YYYY-MM-DD CODE 名称 action 金额 份额 净值 手续费`
   - **非结构化文本**：自动提取日期、金额、买卖方向、基金名称，正则匹配基金代码
   - **名称→代码匹配**：通过 akshare 全市场基金表按名称搜索代码，支持括号全角/半角、冗余词汇自动剥离
   - **LLM 解析**：可配置 DeepSeek 处理复杂 OCR 文本，自动识别不同平台导出格式
4. **ETF 自动过滤** — 导入时通过 akshare 基金类型字段识别场内 ETF 并静默跳过
5. **候选审查** — 所有解析结果进入候选队列，可编辑/确认/忽略。缺代码的候选提供"搜索代码"按钮一键匹配

### 自动计算

- **买入**：有金额无份额 → `份额 = (金额 - 手续费) / 净值`；反之反向推算
- **卖出**：有份额无金额 → `金额 = 份额 × 净值`；自动按 FIFO 持仓天数匹配赎回费率
- **确认日**：根据基金规则的 T+N 和下单截止时间自动推算
- **精度**：份额保留 2 位小数（公募基金标准）

### 持仓 & 收益

- 持仓汇总：成本、市值、浮盈亏、收益率
- 已清仓记录：已实现收益、已实现收益率
- 收益曲线：SVG 图表展示每只基金的累计收益率 vs 沪深300基准，标注买入/卖出点
- FIFO 成本归集：卖出时按先进先出原则扣减持仓

### 基金规则

- 自动同步（akshare）：T+N 确认日、申购费率、赎回费率阶梯、基金类型
- 手动维护：代码/名称/确认日/截止时间/费率
- 可折叠展示，每条规则只显示关键信息
- 货币基金特殊处理：净值=1.0、0 申购费、0 赎回费

### 净值 & 基准

- 通过 efinance 同步历史净值
- 沪深300 基准数据通过 akshare（新浪回退）同步

### 备份 & 恢复

- JSON 全量导出（含候选、流水、NAV、规则、设置）
- 密钥字段自动脱敏（导出为 `***`）
- 恢复前预览，upsert 逻辑

### 运行时设置

`/settings` 页面在线修改配置，无需重启服务：
- DeepSeek API（LLM 解析/代码推断）
- OCR 后端选择（rapidocr/paddle/api/baidu）
- OCR API 参数（文件字段、文本路径、鉴权）

## DeepSeek 配置

配置深度求索 API 即可启用 LLM 解析复杂 OCR 文本：

```bash
DEEPSEEK_API_KEY=your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_ENABLED=true
```

添加到 `/etc/fund-ledger.env`，重启服务生效。也可在 `/settings` 页面在线修改。

## 系统服务

```bash
# 查看状态
systemctl status fund-ledger.service

# 重启
systemctl restart fund-ledger.service

# 查看日志
journalctl -u fund-ledger.service --since "5 min ago"
```

## 技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (WAL 模式) |
| ORM | SQLModel |
| 模板 | Jinja2 + HTMX |
| OCR | RapidOCR-Onnxruntime / PaddleOCR / Baidu API |
| LLM | DeepSeek API |
| 基金数据 | efinance（净值）+ akshare（规则/名称/基准）|
| 前端 | 原生 CSS（深色/浅色主题，响应式）|
