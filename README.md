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
