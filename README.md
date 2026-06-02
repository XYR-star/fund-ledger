# Fund Ledger

Personal China OTC fund ledger prototype.

## Paths

- Project: `/www/projects/fund-ledger`
- Data: `/www/data/fund-ledger`
- SQLite: `/www/data/fund-ledger/fund-ledger.sqlite3`
- Uploads: `/www/data/fund-ledger/uploads`
- NAV cache: `/www/data/fund-ledger/cache/nav`

## Local Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example /etc/fund-ledger.env
uvicorn app.main:app --host 127.0.0.1 --port 4330
```

## Import Pipeline

- Upload images or PDFs from `/upload`, then open the created import document.
- Run local OCR on the import document to fill the OCR text area.
- Use rule parsing for normalized text lines, or configure DeepSeek and use LLM parsing for messy OCR text.
- Confirm generated candidates from `/candidates` before they enter the formal ledger.

The default OCR backend is `rapidocr`, which is lighter and works on this VPS CPU. The app also keeps an optional `OCR_BACKEND=paddle` code path for machines where PaddleOCR CPU wheels are supported.

DeepSeek is optional. Add these to `/etc/fund-ledger.env` and restart `fund-ledger.service`:

```bash
DEEPSEEK_API_KEY=your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```
