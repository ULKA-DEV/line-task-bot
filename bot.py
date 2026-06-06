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
        f"🌸 อรุณสวัสดิ์ค่า! มาเบลมาแล้วนะคะ ✨",
        f"📋 สรุปงานวันนี้ {today}",
        f"{'─'*30}",
        f"⬜ รอดำเนินการ: {len(todo)} งานค่ะ",
        f"🔄 กำลังทำ: {len(doing)} งานค่ะ",
        f"{'─'*30}",
    ]
    if doing:
        lines.append("🔄 งานที่พี่กำลังทำอยู่นะคะ:")
        for t in doing:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")
    if todo:
        lines.append("⬜ งานที่ยังรออยู่ค่า:")
        for t in todo:
            a = f' ({t[3]})' if t[3] else ''
            lines.append(f"  [{t[0]}] {t[2]}{a}")
    if not todo and not doing:
        lines.append("🎉 ว้าว! ไม่มีงานค้างเลย วันนี้สบายใจได้เลยนะคะ~ 💕")
    return '\n'.join(lines)


HELP_TEXT = """🎀 สวัสดีค่า! หนูมาเบลเลขาสุดน่ารักมาแล้วนะคะ
─────────────────────
📌 เพิ่มงาน
  เพิ่มงาน [ชื่องาน]
  เพิ่มงาน [ชื่องาน] @[ชื่อคน]
  เพิ่มงาน งาน1 | งาน2 | งาน3

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
        lines = text.split('\n')[1:]
        results = []
        STOP_WORDS = ['หมดแล้ว', 'เสร็จแล้ว', 'หมด', 'จบ', 'end']
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # หยุดเมื่อเจอคำสิ้นสุด
            if line.lower() in STOP_WORDS:
                break
            # ตัดเลขนำหน้าออก เช่น "1." "1)" "1 "
            item = re.sub(r'^[\d]+[\.\)]\s*', '', line).strip()
            if not item:
                continue
            assignee = None
            if ' @' in item:
                parts = item.rsplit(' @', 1)
                item = parts[0].strip()
                assignee = parts[1].strip()
            task_id = add_task(group_id, item, assignee)
            a_str = f' (@{assignee})' if assignee else ''
            results.append(f"[{task_id}] {item}{a_str}")
        if not results:
            return "กรุณาระบุชื่องานด้วยนะคะ 🙏"
        ids = ', '.join([r.split(']')[0].replace('[', '') for r in results])
        return f"เรียบร้อยค่า! เพิ่ม {len(results)} งานแล้วนะคะ 📝\nเลขงาน: {ids}\nพิมพ์ งานทั้งหมด เพื่อดูรายการค่า~"

    # เพิ่มงานแบบปกติ (คั่นด้วย |)
    if text.startswith('เพิ่มงาน ') or text.startswith('+ '):
        raw = text[5:].strip() if text.startswith('เพิ่มงาน') else text[2:].strip()
        if not raw:
            return "กรุณาระบุชื่องานด้วยนะคะ เช่น: เพิ่มงาน งาน1 | งาน2 | งาน3 ค่า 🙏"
        if '|' in raw:
            items = [i.strip() for i in raw.split('|') if i.strip()]
        else:
            items = [raw]
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
            tid = results[0].strip().split(']')[0].replace('✅ [', '')
            name = results[0].strip().split('] ', 1)[1] if '] ' in results[0] else ''
            return f"โอเคค่า! เพิ่มงาน [{tid}] {name} ให้แล้วนะคะ 📝"
        id_list = ', '.join([r.strip().split(']')[0].replace('✅ [', '') for r in results])
        return f"เรียบร้อยค่า! เพิ่ม {len(results)} งานแล้วนะคะ 📝\n(เลขงาน: {id_list})\nพิมพ์ งานทั้งหมด เพื่อดูรายการค่า~"

    elif text.startswith('ลบงาน '):
        try:
            tid = int(text.replace('ลบงาน ', '', 1).strip())
            task = get_task_by_id(tid, group_id)
            if not task:
                return f"หาไม่เจอเลยค่า งาน [{tid}] ไม่มีในระบบนะคะ 🥺"
            delete_task(tid, group_id)
            return f"🗑️ ลบงาน [{tid}] {task[2]} ออกแล้วนะคะ~"
        except ValueError:
            return "บอกเลขงานด้วยนะคะ เช่น: ลบงาน 3 ค่า 🙏"

    elif text.startswith('ทำอยู่ '):
        try:
            tid = int(text.replace('ทำอยู่ ', '', 1).strip())
            ok = update_task_status(tid, group_id, 'doing')
            if not ok:
                return f"หาไม่เจอเลยค่า งาน [{tid}] ไม่มีในระบบนะคะ 🥺"
            task = get_task_by_id(tid, group_id)
            return f"🔄 โอเคค่า! [{tid}] {task[2]} กำลังทำอยู่นะคะ สู้ๆ นะคะ! 💪"
        except ValueError:
            return "บอกเลขงานด้วยนะคะ เช่น: ทำอยู่ 3 ค่า 🙏"

    elif text.startswith('เสร็จ '):
        try:
            tid = int(text.replace('เสร็จ ', '', 1).strip())
            ok = update_task_status(tid, group_id, 'done')
            if not ok:
                return f"หาไม่เจอเลยค่า งาน [{tid}] ไม่มีในระบบนะคะ 🥺"
            task = get_task_by_id(tid, group_id)
            return f"เก่งมากเลยค่า! 🎉 งาน [{tid}] {task[2]} เสร็จแล้วนะคะ ยอดเยี่ยมมากค่า~ ✨"
        except ValueError:
            return "บอกเลขงานด้วยนะคะ เช่น: เสร็จ 3 ค่า 🙏"

    elif text.startswith('ยกเลิก '):
        try:
            tid = int(text.replace('ยกเลิก ', '', 1).strip())
            ok = update_task_status(tid, group_id, 'todo')
            if not ok:
                return f"หาไม่เจอเลยค่า งาน [{tid}] ไม่มีในระบบนะคะ 🥺"
            task = get_task_by_id(tid, group_id)
            return f"↩️ โอเคค่า รีเซ็ต [{tid}] {task[2]} กลับไปรอดำเนินการแล้วนะคะ"
        except ValueError:
            return "บอกเลขงานด้วยนะคะ เช่น: ยกเลิก 3 ค่า 🙏"

    elif text in ['งานทั้งหมด', 'ดูงาน', 'งาน']:
        tasks = get_tasks(group_id)
        header = f"📋 งานทั้งหมดเลยค่า ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)

    elif text in ['งานค้าง', 'ยังไม่ทำ', 'todo']:
        tasks = get_tasks(group_id, 'todo') + get_tasks(group_id, 'doing')
        header = f"⏳ งานที่ยังค้างอยู่นะคะ ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)

    elif text in ['งานเสร็จ', 'เสร็จแล้ว', 'done']:
        tasks = get_tasks(group_id, 'done')
        header = f"✅ งานที่เสร็จแล้วค่า ({len(tasks)} รายการ)\n{'─'*25}\n"
        return header + format_task_list(tasks)

    elif text in ['สรุปวันนี้', 'สรุป', 'summary']:
        all_tasks = get_tasks(group_id)
        todo = [t for t in all_tasks if t[4] == 'todo']
        doing = [t for t in all_tasks if t[4] == 'doing']
        done = [t for t in all_tasks if t[4] == 'done']
        today = date.today().strftime('%d/%m/%Y')
        lines = [
            f"📋 มาเบลสรุปงานให้นะคะ — {today}",
            f"{'─'*30}",
            f"⬜ รอดำเนินการ: {len(todo)} งานค่ะ",
            f"🔄 กำลังทำ: {len(doing)} งานค่ะ",
            f"✅ เสร็จแล้ว: {len(done)} งานค่ะ",
            f"{'─'*30}",
        ]
        if doing:
            lines.append("🔄 กำลังทำอยู่นะคะ:")
            for t in doing:
                a = f' ({t[3]})' if t[3] else ''
                lines.append(f"  [{t[0]}] {t[2]}{a}")
        if todo:
            lines.append("⬜ ยังรออยู่เลยค่า:")
            for t in todo:
                a = f' ({t[3]})' if t[3] else ''
                lines.append(f"  [{t[0]}] {t[2]}{a}")
        if done:
            lines.append("✅ เสร็จแล้วค่า เก่งมากเลย!:")
            for t in done:
                a = f' ({t[3]})' if t[3] else ''
                lines.append(f"  [{t[0]}] {t[2]}{a}")
        return '\n'.join(lines)

    elif text in ['ช่วยเหลือ', 'help', '?', 'คำสั่ง']:
        return HELP_TEXT

    elif text in ['ล้างงาน', 'ล้างทั้งหมด', 'reset']:
        conn = get_conn()
        c = conn.cursor()
        c.execute('DELETE FROM tasks WHERE group_id=%s', (group_id,))
        # reset sequence กลับเป็น 1 จริงๆ
        c.execute("ALTER SEQUENCE tasks_id_seq RESTART WITH 1")
        conn.commit()
        conn.close()
        return "🗑️ ล้างงานทั้งหมดแล้วนะคะ เลขงานจะเริ่มใหม่จาก 1 เลยค่า~ ✨"

    elif text in ['มาเบล', 'Mabel', 'mabel', 'เบล']:
        return f"""สวัสดีค่า {sender_name}! 🌸 มาเบลอยู่ตรงนี้นะคะ

มีอะไรให้มาเบลช่วยไหมคะ? พิมพ์สิ่งที่ต้องการได้เลยนะคะ เช่น

📌 เพิ่มงาน [ชื่องาน]
📊 ทำอยู่ / เสร็จ / ยกเลิก [เลขงาน]
📋 งานทั้งหมด / งานค้าง / สรุปวันนี้
🗑️ ลบงาน [เลขงาน]

หรือพิมพ์ว่า ช่วยเหลือ เพื่อดูคำสั่งทั้งหมดนะคะ 💕"""

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


@app.route("/reset-db", methods=['GET'])
def reset_db():
    token = request.args.get('token', '')
    if token != MORNING_ALERT_TOKEN:
        abort(403)
    conn = get_conn()
    c = conn.cursor()
    c.execute('TRUNCATE tasks RESTART IDENTITY')
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'message': 'Database cleared!'})


@app.route("/", methods=['GET'])
def index():
    return "LINE Task Bot is running! 🤖"


init_db()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
