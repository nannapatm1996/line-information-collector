"""
End-of-day consolidated report.

Collects today's LINE reports from SQL, then sends:
  - an email with a summary + Excel attachment   (if SMTP_* settings exist)
  - one LINE push message to a specific account  (if REPORT_LINE_TO exists)

Usage:
  python daily_report.py              -> report for today (REPORT_TZ)
  python daily_report.py 2026-10-15   -> report for a specific day (re-send / backfill)

Schedule it once a day, e.g. 18:00 Thailand time:
  Linux cron:   0 18 * * *  cd /path/to/line-collector && python daily_report.py
  Windows:      Task Scheduler -> daily -> "python daily_report.py" (start in project folder)
"""

import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

TZ = ZoneInfo(os.getenv("REPORT_TZ", "Asia/Bangkok"))
engine = create_engine(os.environ["DATABASE_URL"])

# ---------- 1. load the day's data ----------

day = (datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
       if len(sys.argv) > 1 else datetime.now(TZ).date())
start_local = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
end_local = start_local + timedelta(days=1)

query = text("""
    SELECT m.received_at, r.report_type, r.machine, r.shift, r.quantity, r.unit,
           r.defect_or_reason, r.note, r.needs_review,
           m.message_text AS original_message
    FROM production_reports r
    JOIN line_raw_messages m ON m.id = r.raw_id
    WHERE m.received_at >= :start AND m.received_at < :end
    ORDER BY m.received_at
""")
# received_at is stored in UTC
df = pd.read_sql(query, engine, params={
    "start": start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
    "end": end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
})
if not df.empty:
    df["received_at"] = (pd.to_datetime(df["received_at"]).dt.tz_localize("UTC")
                         .dt.tz_convert(TZ).dt.tz_localize(None))

# ---------- 2. build the summary ----------

def build_summary(df: pd.DataFrame) -> str:
    lines = [f"📋 รายงานประจำวัน {day:%d/%m/%Y}", f"รวม {len(df)} รายการ"]
    if df.empty:
        lines.append("ไม่มีรายงานวันนี้")
        return "\n".join(lines)

    labels = {"defect": "ของเสีย/ตำหนิ", "downtime": "เครื่องหยุด",
              "output": "ยอดผลิต", "other": "อื่นๆ"}
    for rtype, g in df.groupby("report_type"):
        lines.append(f"\n▶ {labels.get(rtype, rtype)}: {len(g)} รายการ")
        agg = (g.groupby(["machine", "defect_or_reason", "unit"], dropna=False)["quantity"]
                .sum(min_count=1).reset_index()
                .sort_values("quantity", ascending=False).head(10))
        def s(v):
            return "-" if pd.isna(v) or v == "" else str(v)
        for _, r in agg.iterrows():
            qty = "-" if pd.isna(r["quantity"]) else f"{float(r['quantity']):g}"
            unit = "" if pd.isna(r["unit"]) else r["unit"]
            lines.append(f"  • {s(r['machine'])} | {s(r['defect_or_reason'])} | "
                         f"{qty} {unit}".rstrip())

    n_review = int(df["needs_review"].astype(bool).sum())
    if n_review:
        lines.append(f"\n⚠️ ต้องตรวจสอบ {n_review} รายการ (ข้อมูลไม่ครบ) — ดูในไฟล์ Excel")
    return "\n".join(lines)


summary = build_summary(df)
print(summary)

# ---------- 3. Excel attachment ----------

def build_excel(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="All reports", index=False)
        review = df[df["needs_review"].astype(bool)] if not df.empty else df
        review.to_excel(xw, sheet_name="Needs review", index=False)
    return buf.getvalue()

# ---------- 4. send email ----------

def send_email(summary: str, excel: bytes):
    host = os.getenv("SMTP_HOST")
    to = os.getenv("REPORT_EMAIL_TO")
    if not (host and to):
        print("Email skipped (SMTP_HOST / REPORT_EMAIL_TO not set)")
        return
    msg = EmailMessage()
    msg["Subject"] = f"Production report {day:%Y-%m-%d} ({len(df)} items)"
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER"))
    msg["To"] = to  # comma-separated for several recipients
    msg.set_content(summary)
    msg.add_attachment(excel, maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=f"production_report_{day:%Y%m%d}.xlsx")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
    print(f"Email sent to {to}")

# ---------- 5. send LINE push ----------

def send_line(summary: str):
    to = os.getenv("REPORT_LINE_TO")  # a userId (U...) or groupId (C...)
    if not to:
        print("LINE push skipped (REPORT_LINE_TO not set)")
        return
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN']}"},
        json={"to": to, "messages": [{"type": "text", "text": summary[:4900]}]},
        timeout=15,
    )
    r.raise_for_status()
    print("LINE push sent")


if __name__ == "__main__":
    send_email(summary, build_excel(df))
    send_line(summary)
