"""
LINE chat -> SQL collector (starter)

Flow:
  LINE message -> webhook (this app) -> verify signature -> save raw text
  -> extract fields (LLM) -> save structured row   (silent: no chat reply)
  End of day: daily_report.py emails / LINE-pushes one consolidated report.

Works with SQL Server or PostgreSQL (set DATABASE_URL in .env).
Run:  python -m uvicorn main:app --host 127.0.0.1 --port 8000
Expose over HTTPS (Tailscale Funnel or Cloudflare Tunnel) and set
https://<your-host>/webhook as the webhook URL in the LINE Developers console.
"""

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, MetaData,
                        Numeric, Table, Unicode, UnicodeText, create_engine, insert)

load_dotenv()

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
DATABASE_URL = os.environ["DATABASE_URL"]  # e.g. postgresql+psycopg2://user:pw@host/db
LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-5-5")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
llm = Anthropic()  # reads ANTHROPIC_API_KEY
app = FastAPI()


# ---------- database ----------
# Defined with SQLAlchemy types so the same code works on SQL Server and PostgreSQL.
# Unicode/UnicodeText become NVARCHAR on SQL Server, so Thai text is stored correctly.
# Times are stored as UTC without a time zone.

meta = MetaData()

raw_messages = Table(
    "line_raw_messages", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("received_at", DateTime, nullable=False),
    Column("line_user_id", Unicode(64)),
    Column("line_group_id", Unicode(64)),
    Column("message_text", UnicodeText, nullable=False),
)

production_reports = Table(
    "production_reports", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("raw_id", Integer, ForeignKey("line_raw_messages.id")),
    Column("report_type", Unicode(20)),        # defect / downtime / output / other
    Column("machine", Unicode(100)),
    Column("shift", Unicode(20)),
    Column("quantity", Numeric(18, 3)),
    Column("unit", Unicode(20)),
    Column("defect_or_reason", Unicode(200)),
    Column("note", UnicodeText),
    Column("needs_review", Boolean, default=False),
    Column("created_at", DateTime, nullable=False),
)

# Creates the tables if they don't exist. Raw messages are ALWAYS kept,
# so nothing is lost if extraction is wrong; you can re-parse later.
meta.create_all(engine)


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def save_raw(received_at, user_id, group_id, msg) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            insert(raw_messages).returning(raw_messages.c.id),
            {"received_at": received_at, "line_user_id": user_id,
             "line_group_id": group_id, "message_text": msg},
        )
        return result.scalar_one()


def to_number(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def save_report(raw_id: int, data: dict):
    with engine.begin() as conn:
        conn.execute(insert(production_reports), {
            "raw_id": raw_id,
            "report_type": data.get("report_type"),
            "machine": data.get("machine"),
            "shift": data.get("shift"),
            "quantity": to_number(data.get("quantity")),
            "unit": data.get("unit"),
            "defect_or_reason": data.get("defect_or_reason"),
            "note": data.get("note"),
            "needs_review": bool(data.get("needs_review")),
            "created_at": utc_now(),
        })


# ---------- extraction ----------

EXTRACT_PROMPT = """You extract production data from a factory chat message (Thai or English).
Return ONLY a JSON object with these keys:
  report_type: "defect" | "downtime" | "output" | "other"
  machine: machine/line name or null
  shift: "day" | "night" | null
  quantity: number or null
  unit: e.g. "m", "kg", "pcs", "min" or null
  defect_or_reason: defect type or downtime reason, or null
  note: short summary in English
  needs_review: true if the message is ambiguous or key fields are missing
Do not guess values that are not in the message; use null.

Message:
"""


def extract(message: str) -> dict:
    resp = llm.messages.create(
        model=LLM_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": EXTRACT_PROMPT + message}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"report_type": "other", "note": message[:200], "needs_review": True}


# ---------- LINE ----------

def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str = Header(None)):
    body = await request.body()
    if not verify_signature(body, x_line_signature):
        raise HTTPException(status_code=400, detail="bad signature")

    for event in json.loads(body).get("events", []):
        if event.get("type") != "message" or event["message"].get("type") != "text":
            continue

        msg = event["message"]["text"]
        src = event.get("source", {})
        received_at = datetime.fromtimestamp(event["timestamp"] / 1000, tz=timezone.utc).replace(tzinfo=None)

        raw_id = save_raw(received_at, src.get("userId"), src.get("groupId"), msg)

        data = extract(msg)
        if data.get("report_type") == "other" and not data.get("needs_review"):
            continue  # normal chatter: keep raw text only
        save_report(raw_id, data)
        # Silent mode: no reply in the chat. daily_report.py sends one
        # consolidated report at end of day (email and/or LINE push).

    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}
