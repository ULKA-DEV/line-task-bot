import os
import json
import sqlite3
from datetime import datetime, date
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    FlexSendMessage, BubbleContainer, BoxComponent,
    TextComponent, ButtonComponent, URIAction
)

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

DB_PATH = 'tasks.db'


# ─── Database ───────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            title TEXT NOT NULL,
            assignee TEXT,
            status TEXT DEFAULT 'todo',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def get_tasks(group_id, status=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if status:
        c.execute('SELECT * FROM tasks WHERE group_id=? AND status=? ORDER BY id', (group_id, status))
    else:
        c.execute('SELECT * FROM tasks WHERE group_id=? ORDER BY id', (group_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def add_task(group_id, title, assignee=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO tasks (group_id, title, assignee, status) VALUES (?,?,?,?)',
              (group_id, title, assignee, 'todo'))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id


def update_task_status(task_id, group_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND group_id=?',
              (status, task_id, group_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def delete_task(task_id, group_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM tasks WHERE id=? AND group_id=?', (task_id, group_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def get_task_by_id(task_id, group_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM tasks WHERE id=? AND group_id=?', (task_id, group_id))
    row = c.fetchone()
    conn.close()
    return row


# ─── Message formatters ─────────────────────────────────────
STATUS_EMOJI = {
    'todo': '⬜',
    'doing': '🔄',
    'done': '✅'
}

STATUS_LABEL = {
    'todo': 'รอดำเนินการ',
    'doing': 'กำลังทำ',
    'done': 'เสร็จแล้ว'
}


def format_task_list(tasks):
    if not tasks:
        return "ไม่มีงานในขณะนี้ 🎉"

    lines = []
    for t in tasks:
        tid, gid, title, assignee, status, created, updated = t
        emoji = STATUS_EMOJI.get(status, '⬜')
        assignee_str = f' ({assignee})' if assignee else ''
        lines.append(f"{emoji} [{tid}] {title}{assignee_str}")

    return '\n'.join(lines)


def format_summary(group_id):
    all_tasks = get_tasks(group_id)
    todo = [t for t in all_tasks if t[4] == 'todo']
    doing = [t for t in all_tasks if t[4] == 'doing']
    done = [t for t in all_tasks if t[4] == 'done']

    today = date.today().strftime('%d/%m/%Y')
    lines = [
        f"📋 สรุปงานประจำวัน — {today}",
        f"{'─'*30}",
        f"⬜ รอดำเนินการ: {len(todo)} งาน",
        f"🔄 กำลังทำ: {len(doing)} งาน",
        f"✅ เสร็จแล้ว: {len(done)} งาน",
        f"{'─'*30}",
    ]

    if doing:
        lines.append("🔄 กำลังทำอยู่:")
        for t in doing:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")

    if todo:
        lines.append("⬜ ยังไม่ได้ทำ:")
        for t in todo:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")

    if done:
        lines.append("✅ เสร็จวันนี้:")
        for t in done:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")

    return '\n'.join(lines)


HELP_TEXT = """🤖 คำสั่งบอทเลขา
─────────────────────
📌 จัดการงาน
  เพิ่มงาน [ชื่องาน]
  เพิ่มงาน [ชื่องาน] @[ชื่อคน]
  ลบงาน [เลขงาน]

📊 อัพเดทสถานะ
  ทำอยู่ [เลขงาน]
  เสร็จ [เลขงาน]
  ยกเลิก [เลขงาน]

📋 ดูงาน
  งานทั้งหมด
  งานค้าง
  งานเสร็จ
  สรุปวันนี้

❓ ดูคำสั่ง
  ช่วยเหลือ"""


# ─── Command handler ────────────────────────────────────────
def handle_command(text, group_id, sender_name):
    text = text.strip()
    lower = text.lower()

    # เพิ่มงาน
    if text.startswith('เพิ่มงาน ') or text.startswith('+ '):
        raw = text[5:].strip() if text.startswith('เพิ่มงาน') else text[2:].strip()
        assignee = None
        if ' @' in raw:
            parts = raw.rsplit(' @', 1)
            raw = parts[0].strip()
            assignee = parts[1].strip()
        if not raw:
            return "กรุณาระบุชื่องาน เช่น: เพิ่มงาน ส่งรายงาน @สมชาย"
        task_id = add_task(group_id, raw, assignee)
        a_str = f' มอบหมายให้ @{assignee}' if assignee else ''
        return f"✅ เพิ่มงาน [{task_id}] {raw}{a_str} แล้ว"

    # ลบงาน
    elif text.startswith('ลบงาน '):
        try:
            tid = int(text[5:].strip())
            task = get_task_by_id(tid, group_id)
            if not task:
                return f"❌ ไม่พบงาน [{tid}]"
            delete_task(tid, group_id)
            return f"🗑️ ลบงาน [{tid}] {task[2]} แล้ว"
        except ValueError:
            return "❌ กรุณาระบุเลขงาน เช่น: ลบงาน 3"

    # ทำอยู่
    elif text.startswith('ทำอยู่ '):
        try:
            tid = int(text[5:].strip())
            ok = update_task_status(tid, group_id, 'doing')
            if not ok:
                return f"❌ ไม่พบงาน [{tid}]"
            task = get_task_by_id(tid, group_id)
            return f"🔄 อัพเดท [{tid}] {task[2]} → กำลังทำ"
        except ValueError:
            return "❌ กรุณาระบุเลขงาน เช่น: ทำอยู่ 3"

    # เสร็จ
    elif text.startswith('เสร็จ '):
        try:
            tid = int(text[4:].strip())
            ok = update_task_status(tid, group_id, 'done')
            if not ok:
                return f"❌ ไม่พบงาน [{tid}]"
            task = get_task_by_id(tid, group_id)
            return f"✅ เยี่ยม! งาน [{tid}] {task[2]} เสร็จแล้ว 🎉"
        except ValueError:
            return "❌ กรุณาระบุเลขงาน เช่น: เสร็จ 3"

    # ยกเลิก (reset กลับเป็น todo)
    elif text.startswith('ยกเลิก '):
        try:
            tid = int(text[5:].strip())
            ok = update_task_status(tid, group_id, 'todo')
            if not ok:
                return f"❌ ไม่พบงาน [{tid}]"
            task = get_task_by_id(tid, group_id)
            return f"↩️ รีเซ็ต [{tid}] {task[2]} → รอดำเนินการ"
        except ValueError:
            return "❌ กรุณาระบุเลขงาน เช่น: ยกเลิก 3"

    # ดูงานทั้งหมด
    elif text in ['งานทั้งหมด', 'ดูงาน', 'งาน']:
        tasks = get_tasks(group_id)
        header = f"📋 งานทั้งหมด ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)

    # งานค้าง
    elif text in ['งานค้าง', 'ยังไม่ทำ', 'todo']:
        tasks = get_tasks(group_id, 'todo') + get_tasks(group_id, 'doing')
        header = f"⏳ งานที่ยังไม่เสร็จ ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)

    # งานเสร็จ
    elif text in ['งานเสร็จ', 'เสร็จแล้ว', 'done']:
        tasks = get_tasks(group_id, 'done')
        header = f"✅ งานที่เสร็จแล้ว ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)

    # สรุปวันนี้
    elif text in ['สรุปวันนี้', 'สรุป', 'summary']:
        return format_summary(group_id)

    # ช่วยเหลือ
    elif text in ['ช่วยเหลือ', 'help', '?', 'คำสั่ง']:
        return HELP_TEXT

    return None  # ไม่ตอบถ้าไม่ใช่คำสั่ง


# ─── Webhook ────────────────────────────────────────────────
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # รองรับทั้ง group และ room และ user
    source = event.source
    if source.type == 'group':
        group_id = source.group_id
    elif source.type == 'room':
        group_id = source.room_id
    else:
        group_id = source.user_id  # DM ก็ใช้งานได้

    user_id = source.user_id
    text = event.message.text

    try:
        profile = line_bot_api.get_profile(user_id)
        sender_name = profile.display_name
    except Exception:
        sender_name = 'ผู้ใช้'

    reply = handle_command(text, group_id, sender_name)
    if reply:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )


@app.route("/", methods=['GET'])
def index():
    return "LINE Task Bot is running! 🤖"


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
