# Import and OCR Roadmap

## Goal

Support bulk import from screenshots, PDFs, CSV, and XLSX while keeping all extracted transactions in the candidate queue for manual review before they affect holdings.

## Recommended OCR Order

1. PDF text extraction first.
   - Use `pypdf` or `pdfplumber` for PDFs with selectable text.
   - If extracted text is too short, route the file to OCR.

2. Cloud OCR adapter second.
   - Keep provider-agnostic interface: `recognize(file) -> OcrResult`.
   - Good first candidates: Baidu OCR or Aliyun OCR for Chinese text/table support.
   - Baimiao can be added if stable API documentation and credentials are available.

3. Local OCR fallback later.
   - PaddleOCR is the strongest local Chinese OCR candidate.
   - It is heavier to install, so treat it as an optional worker rather than blocking the web app.

## Parser Pipeline

All input types should feed the same pipeline:

```text
file/text -> raw text + blocks -> source detector -> transaction candidate parser -> pending candidates
```

The parser should not directly create confirmed transactions.

## File Adapters

- `text`: current manual text parser.
- `pdf`: extract selectable text, fallback to OCR.
- `image`: OCR provider.
- `csv`: parse rows via pandas/csv and map columns.
- `xlsx`: parse sheets via pandas/openpyxl and map columns.

## Transaction Coverage

Add explicit parser support for:

- buy / subscription
- sell / redemption
- cash dividend
- dividend reinvestment
- fee adjustment

For unclear rows, create candidates with low confidence and leave missing fields editable.

## Backup

Current `/backup/export` downloads JSON with candidates, confirmed transactions, and NAV records. Next step is a matching restore/import endpoint with preview before applying.
