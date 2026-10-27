# LINE Production Report Collector

Silently collects production reports posted in department LINE groups, turns them into structured data in SQL with AI, and sends **one consolidated report at the end of each day**.

```
Worker posts in department LINE group
   → LINE servers
   → HTTPS webhook (main.py on the Windows Server)
   → department looked up from the group
   → AI extracts the fields (Claude Haiku)
   → SQL Server
End of day: daily_report.py → email (summary + Excel) and/or LINE push
```

---

## Features

### 1. Silent collection
- The bot sits in each department's LINE group and **never replies in the chat**.
- Every message is saved in its original form (`line_raw_messages`), so nothing is lost even if extraction is wrong.
- Casual chat (e.g. "กินข้าวยัง") is stored but not counted as a report.

### 2. Department from the LINE group
- One LINE group = one department, so the department is always right and never guessed.
- New groups register themselves automatically in `line_groups`, including their name from LINE.
- You set each group's `department` once in SQL.

### 3. AI extraction (Claude Haiku)
- Extracts: **report type** (defect / downtime / output), **machine**, **shift**, **quantity**, **unit**, **defect or reason**.
- Handles Thai, English and mixed messages.
- Optional per-department **context** (official machine names and defect codes from `DIM_WC`) keeps names consistent, so they group and join in Power BI.
- Unclear messages are flagged **`needs_review`**.
- If the AI is down or out of credit, the message is still saved and flagged.

### 4. Defect photos
- Downloaded immediately (LINE only keeps message content for a limited time).
- Saved under `IMAGE_DIR/<date>/<group>_<messageId>.jpg` and logged in the database.
- Not read by the AI, so photos add no LLM cost.
- Stickers, videos and files are ignored.

### 5. End-of-day report
- Thai summary grouped by **department, then report type**, with the top machines and defects, the photo count and the number of rows needing review.
- Sent by **email with an Excel attachment** (sheets: *All reports*, *By department*, *Needs review*, *Images*), by **LINE push** to one account, or both.
- Runs on a schedule and can be re-run for any past date.

### 6. Sandbox and testing
- `.env.sandbox` uses a throwaway SQLite file: no real database, no real LINE groups.
- `simulate_line.py` sends fake signed LINE messages from any department group, including a file of real past messages.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Webhook: receives LINE events, saves messages and images, runs AI extraction |
| `daily_report.py` | End-of-day summary + Excel, sent by email and/or LINE push |
| `export_excel.py` | Ad-hoc export of all reports to `reports.xlsx` |
| `simulate_line.py` | Fake LINE messages for testing without LINE |
| `.env.example` | Production settings template |
| `.env.sandbox` | Sandbox settings (SQLite, no real recipients) |
| `requirements.txt` | Python dependencies |

## Database tables (created automatically)

| Table | Contents |
|---|---|
| `line_raw_messages` | Every message as received: time, user, group, text, type (`text`/`image`), image path |
| `line_groups` | One row per LINE group: `group_id`, `group_name`, **`department`**, **`context`** (you fill these two) |
| `production_reports` | Extracted fields per report, with `department` and `needs_review` |

---

## Design decisions

| Topic | Decision |
|---|---|
| Processing | One by one in real time; volume is ~300–400 messages every few days |
| Replies | None in chat, only the end-of-day report |
| Hosting | Existing **Windows Server**, running as a Windows service (NSSM) |
| Database | SQL Server (PostgreSQL also supported); the app uses its own login; database is never exposed |
| Public entry for LINE | Only the webhook is public, via **Cloudflare Tunnel** (company domain) or **Tailscale Funnel** |
| AI model | Claude Haiku 5.5, about **$0.40–1.30 per month** at current volume |
| Native classifier | Not now. Start with AI only, collect labeled data, add a rules-first path later if patterns emerge |
| Context | Start with the official machine and defect lists only; refine from the `needs_review` rows |
| Images | Defect photos only; stored as evidence, no AI reading |

---

## Sandbox test (laptop, no LINE, no real database)

```powershell
cd line-collector
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.sandbox .env            # put your LLM API key in .env
.venv\Scripts\python -m uvicorn main:app --port 8000
```

In a second terminal:

```powershell
.venv\Scripts\python simulate_line.py                                  # sample messages
.venv\Scripts\python simulate_line.py --group Cweave "ทอ5 ผ้าขาด 30 ม."  # one message from a fake group
.venv\Scripts\python simulate_line.py --group Cweave --file messages.txt  # real past messages, one per line
.venv\Scripts\python daily_report.py                                   # prints today's summary
.venv\Scripts\python export_excel.py                                   # writes reports.xlsx
```

- Set a test group's department in `sandbox.db` (table `line_groups`) and send again to see department grouping.
- Delete `sandbox.db` to start fresh (also needed after the table structure changes).

---

## Production setup (Windows Server)

### 1. Create the LINE bot
1. developers.line.biz → create a Provider → create a **Messaging API** channel.
2. Copy the **Channel secret** (Basic settings) and issue a **long-lived Channel access token** (Messaging API tab).
3. In LINE Official Account Manager: turn **on** "Allow bot to join group chats"; turn **off** auto-reply and greeting messages.

### 2. Install on the server
- Python 3.12 (tick "Add to PATH")
- ODBC Driver 18 for SQL Server
- NSSM (put `nssm.exe` in `C:\Windows\System32`)
- Cloudflare Tunnel (`cloudflared`) **or** Tailscale

### 3. Create a database and login
Test first in a separate database, then switch to production:

```sql
CREATE DATABASE line_sandbox;
CREATE LOGIN line_app WITH PASSWORD = 'StrongPassword!';
USE line_sandbox;
CREATE USER line_app FOR LOGIN line_app;
ALTER ROLE db_owner ADD MEMBER line_app;
```

The `line_app` login has no access to any other database.

### 4. Install the project

```powershell
# unzip to C:\apps\line-collector
cd C:\apps\line-collector
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env      # LINE secret + token, LLM key, DATABASE_URL, IMAGE_DIR, email settings
```

Check it starts: run `.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000` and open http://127.0.0.1:8000/health. It should show `{"status":"ok"}`.

### 5. Run as a Windows service

```powershell
nssm install LineCollector "C:\apps\line-collector\.venv\Scripts\python.exe" "-m uvicorn main:app --host 127.0.0.1 --port 8000"
nssm set LineCollector AppDirectory C:\apps\line-collector
nssm set LineCollector AppStdout C:\apps\line-collector\service.log
nssm set LineCollector AppStderr C:\apps\line-collector\service.log
nssm start LineCollector
```

### 6. Give the webhook a public HTTPS URL
- **Cloudflare Tunnel:** `cloudflared service install <token>`, then route e.g. `line.yourcompany.com` to `http://localhost:8000`.
- **Tailscale Funnel:** `tailscale funnel --bg 8000`; `tailscale funnel status` shows the URL.

In the LINE console (Messaging API tab): set Webhook URL to `https://<your-host>/webhook` → **Verify** → turn on **Use webhook**.

### 7. Connect the department groups
1. Add the bot to each department's LINE group. Each group appears in `line_groups`.
2. Set its department and (optionally) context:

```sql
UPDATE line_groups
SET department = N'เครื่องทอ',
    context    = N'เครื่อง: ทอ1–ทอ20; ปัญหา: ผ้าขาด, ด้ายขาด, ลายเพี้ยน'
WHERE group_name = N'<group name>';
```

3. Fill in the department for any reports received before it was set:

```sql
UPDATE r SET r.department = g.department
FROM production_reports r
JOIN line_raw_messages m ON m.id = r.raw_id
JOIN line_groups g ON g.group_id = m.line_group_id
WHERE r.department IS NULL;
```

### 8. Schedule the end-of-day report
1. Test once by hand: `.venv\Scripts\python daily_report.py`. Check the email arrives.
2. Task Scheduler → daily at 18:00:
   - Program: `C:\apps\line-collector\.venv\Scripts\python.exe`
   - Arguments: `daily_report.py`
   - Start in: `C:\apps\line-collector`
   - Tick **"Run whether user is logged on or not"**
3. For the LINE summary: have the recipient message the bot once, copy their `line_user_id` (starts with `U`) from `line_raw_messages` into `REPORT_LINE_TO`.

Re-send a past day: `python daily_report.py 2026-10-15`

### 9. Housekeeping
- Server power plan: **never sleep**; keep it on a UPS. Messages sent while the server is down are lost.
- Back up the database **and** the `images` folder (expect roughly 1–3 GB of photos per year).
- Gmail SMTP needs 2-step verification and an **App Password**.
- Set `REPORT_TZ` to the factory's time zone (`Asia/Bangkok`); it decides where each day starts and ends.
- If webhook verification fails, check `service.log` first.

---

## Rollout path

1. **Sandbox on a laptop:** SQLite + simulator, using exported real chats as the test set.
2. **Test LINE channel and test group:** reports go only to you.
3. **Windows Server with `line_sandbox` database.**
4. **Go live:** switch `DATABASE_URL` and the LINE channel credentials.

## Optional next steps

- Link photos to their text report (same person, same group, within a few minutes)
- Separate report per department head
- Script to parse LINE chat exports (`.txt`) for drafting context and building test sets
- Switch between Claude and GPT for a head-to-head accuracy test
- Log the AI cost of each message
