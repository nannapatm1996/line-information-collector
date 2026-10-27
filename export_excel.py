"""Export collected LINE reports from SQL to Excel.
Usage: python export_excel.py            -> reports.xlsx (all rows)
       python export_excel.py 2026-10-01 -> rows since that date
"""
import os
import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DATABASE_URL"])
since = sys.argv[1] if len(sys.argv) > 1 else "1900-01-01"

query = text("""
    SELECT r.id, m.received_at, r.report_type, r.machine, r.shift, r.quantity,
           r.unit, r.defect_or_reason, r.note, r.needs_review,
           m.message_text AS original_message
    FROM production_reports r
    JOIN line_raw_messages m ON m.id = r.raw_id
    WHERE m.received_at >= :since
    ORDER BY m.received_at
""")

df = pd.read_sql(query, engine, params={"since": since})
df["received_at"] = pd.to_datetime(df["received_at"]).dt.tz_localize(None)

with pd.ExcelWriter("reports.xlsx") as xw:
    df.to_excel(xw, sheet_name="All reports", index=False)
    df[df["needs_review"]].to_excel(xw, sheet_name="Needs review", index=False)

print(f"Exported {len(df)} rows to reports.xlsx")
