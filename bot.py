import os
import re
import psycopg2
from datetime import date
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
 
app = Flask(__name__)
 
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
MORNING_ALERT_TOKEN = os.environ.get('MORNING_ALERT_TOKEN', 'mytoken123')
 
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
 
 
# ─── Database ───────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode='require')
 
 
def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            group_id TEXT NOT NULL,
            title TEXT NOT NULL,
            assignee TEXT,
            status TEXT DEFAULT 'todo',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # เก็บ group_id สำหรับแจ้งเตือน
    c.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
 
 
def register_group(group_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT INTO groups (group_id) VALUES (%s) ON CONFLICT DO NOTHING', (group_id,))
    conn.commit()
    conn.close()
 
 
def get_all_groups():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT group_id FROM groups')
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows
 
 
def get_tasks(group_id, status=None):
    conn = get_conn()
    c = conn.cursor()
    if status:
        c.execute('SELECT * FROM tasks WHERE group_id=%s AND status=%s ORDER BY id', (group_id, status))
    else:
        c.execute('SELECT * FROM tasks WHERE group_id=%s ORDER BY id', (group_id,))
    rows = c.fetchall()
    conn.close()
    return rows
 
 
def add_task(group_id, title, assignee=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute('INSERT INTO tasks (group_id, title, assignee, status) VALUES (%s,%s,%s,%s) RETURNING id',
              (group_id, title, assignee, 'todo'))
    task_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return task_id
 
 
def update_task_status(task_id, group_id, status):
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE tasks SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s AND group_id=%s',
              (status, task_id, group_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0
 
 
def delete_task(task_id, group_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM tasks WHERE id=%s AND group_id=%s', (task_id, group_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0
 
 
def get_task_by_id(task_id, group_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM tasks WHERE id=%s AND group_id=%s', (task_id, group_id))
    row = c.fetchone()
    conn.close()
    return row
 
 
# ─── Formatters ─────────────────────────────────────────────
STATUS_EMOJI = {'todo': '⬜', 'doing': '🔄', 'done': '✅'}
 
 
def format_task_list(tasks):
    if not tasks:
        return "ไม่มีงานในขณะนี้ 🎉"
    lines = []
    for t in tasks:
        emoji = STATUS_EMOJI.get(t[4], '⬜')
        a = f' ({t[3]})' if t[3] else ''
        lines.append(f"{emoji} [{t[0]}] {t[2]}{a}")
    return '\n'.join(lines)
 
 
def format_morning_alert(group_id):
    all_tasks = get_tasks(group_id)
    todo = [t for t in all_tasks if t[4] == 'todo']
    doing = [t for t in all_tasks if t[4] == 'doing']
 
    today = date.today().strftime('%d/%m/%Y')
    lines = [
        f"🌅 อรุณสวัสดิ์! สรุปงานประจำวัน {today}",
        f"{'─'*30}",
        f"⬜ รอดำเนินการ: {len(todo)} งาน",
        f"🔄 กำลังทำ: {len(doing)} งาน",
        f"{'─'*30}",
    ]
    if doing:
        lines.append("🔄 กำลังทำอยู่:")
        for t in doing:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")
    if todo:
        lines.append("⬜ งานที่รอดำเนินการ:")
        for t in todo:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")
    if not todo and not doing:
        lines.append("🎉 ไม่มีงานค้าง วันนี้สบายใจได้เลย!")
    return '\n'.join(lines)
 
 
HELP_TEXT = """🤖 คำสั่งบอทเลขา
─────────────────────
📌 เพิ่มงาน (แบบเดียว)
  เพิ่มงาน ส่งรายงาน
  เพิ่มงาน ส่งรายงาน @สมชาย
 
📌 เพิ่มงาน (หลายงานพร้อมกัน)
  เพิ่มงาน งานที่1, งานที่2, งานที่3
 
  หรือแบบลิสต์:
  เพิ่มงาน
  1 ส่งรายงาน
  2 ประชุมลูกค้า @มานี
  3 อัพเดทเว็บ
 
🗑️ ลบงาน
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
 
    # เพิ่มงานแบบลิสต์ตัวเลข (ขึ้นบรรทัดใหม่)
    if (text.startswith('เพิ่มงาน\n') or text == 'เพิ่มงาน') and '\n' in text:
        lines = text.split('\n')[1:]  # ตัด "เพิ่มงาน" บรรทัดแรกออก
        results = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # ตัดเลขนำหน้าออก เช่น "1 งาน", "1. งาน", "- งาน"
            item = re.sub(r'^[\d\-\*\.]+\s*', '', line).strip()
            if not item:
                continue
            assignee = None
            if ' @' in item:
                parts = item.rsplit(' @', 1)
                item = parts[0].strip()
                assignee = parts[1].strip()
            task_id = add_task(group_id, item, assignee)
            a_str = f' (@{assignee})' if assignee else ''
            results.append(f"  ✅ [{task_id}] {item}{a_str}")
        if not results:
            return "กรุณาระบุชื่องาน"
        return f"✅ เพิ่ม {len(results)} งานแล้ว\n" + '\n'.join(results)
 
    # เพิ่มงานแบบปกติ (คั่นด้วยลูกน้ำ)
    if text.startswith('เพิ่มงาน ') or text.startswith('+ '):
        raw = text[5:].strip() if text.startswith('เพิ่มงาน') else text[2:].strip()
        if not raw:
            return "กรุณาระบุชื่องาน เช่น: เพิ่มงาน ส่งรายงาน, ประชุมลูกค้า"
        items = [i.strip() for i in raw.split(',') if i.strip()]
        results = []
        for item in items:
            assignee = None
            if ' @' in item:
                parts = item.rsplit(' @', 1)
                item = parts[0].strip()
                assignee = parts[1].strip()
            task_id = add_task(group_id, item, assignee)
            a_str = f' (@{assignee})' if assignee else ''
            results.append(f"  ✅ [{task_id}] {item}{a_str}")
        if len(results) == 1:
            return f"✅ เพิ่มงาน {results[0].strip()} แล้ว"
        return f"✅ เพิ่ม {len(results)} งานแล้ว\n" + '\n'.join(results)
 
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
 
    elif text in ['งานทั้งหมด', 'ดูงาน', 'งาน']:
        tasks = get_tasks(group_id)
        header = f"📋 งานทั้งหมด ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)
 
    elif text in ['งานค้าง', 'ยังไม่ทำ', 'todo']:
        tasks = get_tasks(group_id, 'todo') + get_tasks(group_id, 'doing')
        header = f"⏳ งานที่ยังไม่เสร็จ ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)
 
    elif text in ['งานเสร็จ', 'เสร็จแล้ว', 'done']:
        tasks = get_tasks(group_id, 'done')
        header = f"✅ งานที่เสร็จแล้ว ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)
 
    elif text in ['สรุปวันนี้', 'สรุป', 'summary']:
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
 
    elif text in ['ช่วยเหลือ', 'help', '?', 'คำสั่ง']:
        return HELP_TEXT
 
    return None
 
 
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
    source = event.source
    if source.type == 'group':
        group_id = source.group_id
        register_group(group_id)
    elif source.type == 'room':
        group_id = source.room_id
        register_group(group_id)
    else:
        group_id = source.user_id
 
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
 
 
# ─── Morning Alert Endpoint ─────────────────────────────────
@app.route("/morning-alert", methods=['POST'])
def morning_alert():
    token = request.args.get('token', '')
    if token != MORNING_ALERT_TOKEN:
        abort(403)
    groups = get_all_groups()
    for group_id in groups:
        msg = format_morning_alert(group_id)
        try:
            line_bot_api.push_message(group_id, TextSendMessage(text=msg))
        except Exception as e:
            print(f"Error sending to {group_id}: {e}")
    return jsonify({'sent': len(groups)})
 
 
@app.route("/", methods=['GET'])
def index():
    return "LINE Task Bot is running! 🤖"
 
 
init_db()
 
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
 
