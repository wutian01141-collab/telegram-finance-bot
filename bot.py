# ================================
# Telegram 出入款统计机器人（企业版最终稳定版）
# 作者：悟天
# ================================

import os
import re
import sqlite3
import logging
from zoneinfo import ZoneInfo
from datetime import datetime, time, timedelta

from telegram import Update, ReplyKeyboardMarkup, BotCommand
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("请设置 BOT_TOKEN")

DB_FILE = "data.db"
TZ = ZoneInfo("Asia/Bangkok")

# 固定时间
FIXED_SUMMARY = time(10, 0)   # 10:00 固定汇总（不@）
RESET_TIME = time(12, 0)      # 12:00 自动清零
DUP_SECONDS = 10              # 防重复窗口：10秒

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)


# ================================
# 数据库
# ================================
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table_name: str, column_name: str, column_def: str):
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    cols = [r["name"] for r in cur.fetchall()]
    if column_name not in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")


def init_db():
    conn = db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        type TEXT,
        customer TEXT,
        receptionist TEXT DEFAULT '',
        amount REAL,
        is_result INTEGER,
        time TEXT,
        period TEXT,
        source_message_id INTEGER,
        source_user_id INTEGER,
        source_text TEXT DEFAULT ''
    )
    """)

    # 兼容旧表
    ensure_column(conn, "records", "receptionist", "receptionist TEXT DEFAULT ''")
    ensure_column(conn, "records", "source_message_id", "source_message_id INTEGER")
    ensure_column(conn, "records", "source_user_id", "source_user_id INTEGER")
    ensure_column(conn, "records", "source_text", "source_text TEXT DEFAULT ''")

    c.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        chat_type TEXT,
        updated_at TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS alert_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        username TEXT,
        full_name TEXT,
        UNIQUE(chat_id, user_id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS report_times (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        report_time TEXT,
        UNIQUE(chat_id, report_time)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS action_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        action TEXT,
        detail TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


# ================================
# 工具
# ================================
def now():
    return datetime.now(TZ)


def period_key(dt=None):
    dt = dt or now()
    # 每天 12:00 前算前一天周期
    if dt.time() < RESET_TIME:
        base = dt.date() - timedelta(days=1)
    else:
        base = dt.date()
    return base.strftime("%Y-%m-%d")


def period_range_text(p: str):
    d = datetime.strptime(p, "%Y-%m-%d").date()
    nd = d + timedelta(days=1)
    return f"{d.strftime('%Y-%m-%d')} 21:00 ~ {nd.strftime('%Y-%m-%d')} 11:00 (TH)"


def fmt_money(x):
    return f"{x:.2f} USDT"


def message_text_content(message) -> str:
    if not message:
        return ""
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def format_party(receptionist: str, customer: str) -> str:
    receptionist = (receptionist or "").strip()
    customer = (customer or "").strip()
    if receptionist and customer:
        return f"{receptionist}/{customer}"
    return customer or receptionist or "未命名客户"


def ensure_chat(chat_id: int, title: str, chat_type: str):
    conn = db()
    conn.execute("""
    INSERT INTO chats(chat_id, title, chat_type, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(chat_id) DO UPDATE SET
      title=excluded.title,
      chat_type=excluded.chat_type,
      updated_at=excluded.updated_at
    """, (chat_id, title, chat_type, now().isoformat()))
    conn.commit()
    conn.close()


def get_chat_ids():
    conn = db()
    rows = conn.execute("SELECT chat_id FROM chats").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]


def log_action(chat_id: int, user_id: int, action: str, detail: str):
    conn = db()
    conn.execute("""
    INSERT INTO action_logs(chat_id, user_id, action, detail, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (chat_id, user_id, action, detail, now().isoformat()))
    conn.commit()
    conn.close()


# ================================
# 权限
# ================================
async def is_group_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == ChatType.PRIVATE:
        return True
    member = await ctx.bot.get_chat_member(chat.id, user.id)
    return member.status in ("creator", "administrator")


# ================================
# 记录写入 + 防重复
# ================================
def is_duplicate_record(chat_id: int, record_type: str, customer: str, receptionist: str, amount: float) -> bool:
    conn = db()
    row = conn.execute(
        """
        SELECT * FROM records
        WHERE chat_id=? AND type=? AND customer=? AND receptionist=? AND amount=? AND period=? AND amount>0
        ORDER BY id DESC
        LIMIT 1
        """,
        (chat_id, record_type, customer, receptionist or "", amount, period_key()),
    ).fetchone()
    conn.close()

    if not row:
        return False

    try:
        last_time = datetime.fromisoformat(row["time"])
    except Exception:
        return False

    return (now() - last_time).total_seconds() <= DUP_SECONDS


def add_record(chat_id, record_type, customer, receptionist, amount, is_result, source_message_id, source_user_id, source_text):
    conn = db()
    conn.execute(
        """
        INSERT INTO records (
            chat_id, type, customer, receptionist, amount, is_result, time, period,
            source_message_id, source_user_id, source_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            record_type,
            customer,
            receptionist or "",
            amount,
            is_result,
            now().isoformat(),
            period_key(),
            source_message_id,
            source_user_id,
            source_text or "",
        ),
    )
    conn.commit()
    conn.close()


# ================================
# 模板识别
# ================================
def parse_template(text: str):
    """
    模板识别：
    - 识别类型：入金 / 出金 / 出款
    - 识别客户
    - 识别接待
    - 识别 Result / Known -> 新单
    - 模板金额不计入统计
    """
    if not text:
        return None

    record_type = None
    if "入金" in text or "入款" in text:
        record_type = "入金"
    elif "出金" in text or "出款" in text:
        record_type = "出款"
    else:
        return None

    customer_match = re.search(r"客户\s*[:：]\s*(.+)", text)
    if not customer_match:
        return None

    customer_raw = customer_match.group(1).strip().splitlines()[0].strip()
    customer = customer_raw.split("/")[0].strip()

    receptionist = ""
    receptionist_match = re.search(r"接待人姓名\s*[:：]\s*(.+)", text)
    if receptionist_match:
        receptionist = receptionist_match.group(1).strip().splitlines()[0].strip()

    lower_text = text.lower()
    is_result = 1 if "result" in lower_text else 0

    return {
        "type": record_type,
        "customer": customer,
        "receptionist": receptionist,
        "amount": 0.0,
        "is_result": is_result,
        "template_only": True,
    }


def extract_template_info_from_reply(update: Update):
    if not update.message:
        return None

    reply = update.message.reply_to_message
    if not reply:
        return None

    raw_text = message_text_content(reply)
    if not raw_text:
        return None

    customer_match = re.search(r"客户\s*[:：]\s*(.+)", raw_text)
    if not customer_match:
        return None

    customer_raw = customer_match.group(1).strip().splitlines()[0].strip()
    customer = customer_raw.split("/")[0].strip()

    receptionist = ""
    receptionist_match = re.search(r"接待人姓名\s*[:：]\s*(.+)", raw_text)
    if receptionist_match:
        receptionist = receptionist_match.group(1).strip().splitlines()[0].strip()

    return {
        "customer": customer or "未命名客户",
        "receptionist": receptionist or "",
    }


# ================================
# 金额识别
# ================================
def parse_quick_amount(text: str, customer_name: str | None = None, receptionist: str | None = None):
    if not text:
        return None

    t = text.strip()

    # 入款
    in_match = re.search(r"收到\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", t)
    if in_match:
        return {
            "type": "入金",
            "amount": round(float(in_match.group(1)), 2),
            "customer": customer_name or "未命名客户",
            "receptionist": receptionist or "",
            "is_result": 0,
            "template_only": False,
        }

    # 出款
    out_match = re.search(r"(?:已出|出款|出)\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", t)
    if out_match:
        return {
            "type": "出款",
            "amount": round(float(out_match.group(1)), 2),
            "customer": customer_name or "未命名客户",
            "receptionist": receptionist or "",
            "is_result": 0,
            "template_only": False,
        }

    return None


# ================================
# 修改金额（回调）
# 用法：回复原金额消息，发送：
# 修改为20U
# 改为20U
# ================================
def parse_edit_amount(text: str):
    if not text:
        return None
    m = re.search(r"(?:修改为|改为)\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", text.strip())
    if not m:
        return None
    return round(float(m.group(1)), 2)


def find_record_by_replied_message(chat_id: int, reply_message_id: int):
    conn = db()
    row = conn.execute(
        """
        SELECT * FROM records
        WHERE chat_id=? AND source_message_id=? AND amount>0
        ORDER BY id DESC
        LIMIT 1
        """,
        (chat_id, reply_message_id),
    ).fetchone()
    conn.close()
    return row


def update_record_amount(record_id: int, new_amount: float):
    conn = db()
    conn.execute(
        "UPDATE records SET amount=? WHERE id=?",
        (new_amount, record_id),
    )
    conn.commit()
    conn.close()


# ================================
# 提醒人员
# ================================
def add_alert_member(chat_id: int, user_id: int, username: str, full_name: str):
    conn = db()
    conn.execute("""
    INSERT OR IGNORE INTO alert_members(chat_id, user_id, username, full_name)
    VALUES (?, ?, ?, ?)
    """, (chat_id, user_id, username or "", full_name))
    conn.commit()
    conn.close()


def remove_alert_member(chat_id: int, user_id: int):
    conn = db()
    conn.execute("DELETE FROM alert_members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_alert_members(chat_id: int):
    conn = db()
    rows = conn.execute("""
    SELECT user_id, username, full_name
    FROM alert_members
    WHERE chat_id=?
    ORDER BY full_name
    """, (chat_id,)).fetchall()
    conn.close()
    return rows


def mention_text(chat_id: int) -> str:
    rows = get_alert_members(chat_id)
    if not rows:
        return ""
    parts = []
    for r in rows:
        if r["username"]:
            parts.append(f"@{r['username']}")
        else:
            parts.append(f'<a href="tg://user?id={r["user_id"]}">{r["full_name"]}</a>')
    return " ".join(parts)


# ================================
# 指定时间
# ================================
def replace_report_times(chat_id: int, times_list: list[str]):
    conn = db()
    conn.execute("DELETE FROM report_times WHERE chat_id=?", (chat_id,))
    for t in times_list:
        conn.execute("INSERT OR IGNORE INTO report_times(chat_id, report_time) VALUES (?, ?)", (chat_id, t))
    conn.commit()
    conn.close()


def get_report_times(chat_id: int):
    conn = db()
    rows = conn.execute("""
    SELECT report_time FROM report_times
    WHERE chat_id=?
    ORDER BY report_time
    """, (chat_id,)).fetchall()
    conn.close()
    return [r["report_time"] for r in rows]


# ================================
# 汇总
# ================================
def summary_text(chat_id, p):
    rows = db().execute(
        "SELECT * FROM records WHERE chat_id=? AND period=? ORDER BY time ASC",
        (chat_id, p),
    ).fetchall()

    money_rows = [r for r in rows if float(r["amount"] or 0) > 0]

    ins = [r for r in money_rows if r["type"] == "入金"]
    outs = [r for r in money_rows if r["type"] == "出款"]

    total_in = sum(r["amount"] for r in ins)
    total_out = sum(r["amount"] for r in outs)
    net = total_in - total_out
    result_count = sum(r["is_result"] for r in rows)

    def line(r):
        t = datetime.fromisoformat(r["time"]).strftime("%H:%M:%S")
        party = format_party(r["receptionist"], r["customer"])
        return f"{t} | {party} | {fmt_money(r['amount'])}"

    text = []
    text.append("📊 汇总\n")
    text.append(f"📅 周期：{period_range_text(p)}\n")

    # 入款在上
    text.append("💰 入款：")
    text += [line(r) for r in ins] or ["无"]
    text.append(f"总入款：{fmt_money(total_in)}\n")

    # 出款在下
    text.append("💸 出款：")
    text += [line(r) for r in outs] or ["无"]
    text.append(f"总出款：-{fmt_money(total_out)}\n")

    text.append(f"📈 充提差：{net:.2f} USDT")
    text.append(f"🆕 新单：{result_count}")

    return "\n".join(text), net


# ================================
# 按钮
# ================================
def admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["汇总", "当前"],
            ["设置指定时间", "清零"],
        ],
        resize_keyboard=True
    )


# ================================
# 消息处理
# ================================
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = message_text_content(update.message)
    if not text:
        return

    chat_id = update.effective_chat.id
    chat = update.effective_chat
    user = update.effective_user

    ensure_chat(chat.id, chat.title or chat.full_name or str(chat.id), chat.type)
    ensure_scheduled_for_chat(ctx.application, chat.id)

    # 管理员按钮逻辑
    if text == "汇总":
        msg, _ = summary_text(chat_id, period_key())
        await update.message.reply_text(msg)
        return

    if text == "当前":
        msg, _ = summary_text(chat_id, period_key())
        await update.message.reply_text(msg)
        return

    if text == "设置指定时间":
        if await is_group_admin(update, ctx):
            ctx.user_data["awaiting_report_times"] = True
            await update.message.reply_text("请直接发送时间，例如：00:00 04:00 08:30，或发送 /cancel 退出")
        else:
            await update.message.reply_text("只有管理员可以设置指定时间")
        return

    if text == "清零":
        if await is_group_admin(update, ctx):
            conn = db()
            conn.execute("DELETE FROM records WHERE chat_id=?", (chat_id,))
            conn.commit()
            conn.close()
            log_action(chat_id, user.id, "reset", "按钮清零")
            await update.message.reply_text("本周期已清零")
        else:
            await update.message.reply_text("只有管理员可以清零")
        return

    # 等待输入指定时间
    if ctx.user_data.get("awaiting_report_times"):
        if not await is_group_admin(update, ctx):
            ctx.user_data["awaiting_report_times"] = False
            await update.message.reply_text("只有管理员可以设置指定时间")
            return

        times_list = text.split()

        all_valid = True
        if not times_list:
            all_valid = False
        else:
            for t in times_list:
                if not re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", t):
                    all_valid = False
                    break

        if all_valid:
            replace_report_times(chat_id, sorted(set(times_list)))
            ensure_scheduled_for_chat(ctx.application, chat_id)
            ctx.user_data["awaiting_report_times"] = False
            log_action(chat_id, user.id, "set_report_times", " ".join(sorted(set(times_list))))
            await update.message.reply_text("✅ 指定汇总时间已更新：\n" + "\n".join(sorted(set(times_list))))
            return
        else:
            # 自动退出设置模式，继续正常识别消息
            ctx.user_data["awaiting_report_times"] = False

    # 先处理“修改金额”
    new_amount = parse_edit_amount(text)
    if new_amount is not None and update.message.reply_to_message:
        original = find_record_by_replied_message(chat_id, update.message.reply_to_message.message_id)
        if original:
            old_amount = float(original["amount"] or 0)
            update_record_amount(original["id"], new_amount)
            party = format_party(original["receptionist"], original["customer"])
            log_action(chat_id, user.id, "edit_amount", f"{old_amount} -> {new_amount} | {party}")
            await update.message.reply_text(
                f"✅ 已修改成功\n类型：{original['type']}\n接待/客户：{party}\n原金额：{fmt_money(old_amount)}\n新金额：{fmt_money(new_amount)}"
            )
            return

    # 模板识别
    data = parse_template(text)

    # 金额识别，并优先从回复模板继承
    reply_info = extract_template_info_from_reply(update)
    if not data:
        data = parse_quick_amount(
            text,
            customer_name=(reply_info["customer"] if reply_info else None),
            receptionist=(reply_info["receptionist"] if reply_info else None),
        )

    if data:
        # 模板
        if data.get("template_only"):
            add_record(
                chat_id=chat_id,
                record_type=data["type"],
                customer=data["customer"],
                receptionist=data.get("receptionist", ""),
                amount=0.0,
                is_result=data["is_result"],
                source_message_id=update.message.message_id,
                source_user_id=user.id,
                source_text=text,
            )
            msg = (
                f"✅ 已识别模板\n"
                f"接待/客户：{format_party(data.get('receptionist', ''), data['customer'])}\n"
                f"新单：{'是' if data['is_result'] else '否'}\n"
                f"金额不计入统计"
            )
            await update.message.reply_text(msg)
            return

        # 防重复
        if is_duplicate_record(
            chat_id=chat_id,
            record_type=data["type"],
            customer=data["customer"],
            receptionist=data.get("receptionist", ""),
            amount=data["amount"],
        ):
            await update.message.reply_text("⚠️ 疑似重复记录，已忽略")
            return

        add_record(
            chat_id=chat_id,
            record_type=data["type"],
            customer=data["customer"],
            receptionist=data.get("receptionist", ""),
            amount=data["amount"],
            is_result=data["is_result"],
            source_message_id=update.message.message_id,
            source_user_id=user.id,
            source_text=text,
        )

        msg = (
            f"✅ 已记录\n"
            f"类型：{data['type']}\n"
            f"接待/客户：{format_party(data.get('receptionist', ''), data['customer'])}\n"
            f"金额：{fmt_money(data['amount'])}"
        )
        await update.message.reply_text(msg)
        return

    # 直接输入客户名查询当前周期
    rows = db().execute(
        "SELECT DISTINCT customer FROM records WHERE chat_id=? AND period=?",
        (chat_id, period_key()),
    ).fetchall()
    names = [r["customer"] for r in rows]

    if text in names:
        rows = db().execute(
            """
            SELECT * FROM records
            WHERE chat_id=? AND period=? AND customer=?
            ORDER BY time ASC
            """,
            (chat_id, period_key(), text),
        ).fetchall()

        if not rows:
            await update.message.reply_text(f"当前周期未找到客户：{text}")
            return

        total_in = sum(float(r["amount"] or 0) for r in rows if r["type"] == "入金")
        total_out = sum(float(r["amount"] or 0) for r in rows if r["type"] == "出款")
        net = total_in - total_out

        msg = [f"📊 {text}\n"]
        for r in rows:
            party = format_party(r["receptionist"], r["customer"])
            tm = datetime.fromisoformat(r["time"]).strftime("%H:%M:%S")
            msg.append(f"{tm} | {r['type']} | {party} | {fmt_money(r['amount'])}")

        msg.append("")
        msg.append(f"总入款：{fmt_money(total_in)}")
        msg.append(f"总出款：-{fmt_money(total_out)}")
        msg.append(f"充提差：{net:.2f} USDT")

        await update.message.reply_text("\n".join(msg))


# ================================
# 命令
# ================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    ensure_chat(chat.id, chat.title or chat.full_name or str(chat.id), chat.type)
    ensure_scheduled_for_chat(ctx.application, chat.id)

    text = (
        "机器人已启动\n\n"
        "管理员按钮：汇总 / 当前 / 设置指定时间 / 清零\n"
        "支持：图片+文字模板、回复模板继承接待/客户、只统计收到/已出金额、回复原金额消息可修改金额"
    )

    if await is_group_admin(update, ctx):
        await update.message.reply_text(text, reply_markup=admin_keyboard())
    else:
        await update.message.reply_text(text)


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["awaiting_report_times"] = False
    await update.message.reply_text("已退出设置指定时间模式")


async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, _ = summary_text(update.effective_chat.id, period_key())
    await update.message.reply_text(msg)


async def current(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, _ = summary_text(update.effective_chat.id, period_key())
    await update.message.reply_text(msg)


async def details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db().execute(
        """
        SELECT * FROM records
        WHERE chat_id=? AND period=? AND amount>0
        ORDER BY time ASC
        """,
        (update.effective_chat.id, period_key()),
    ).fetchall()

    if not rows:
        await update.message.reply_text("当前周期没有金额记录")
        return

    msg = ["📄 明细"]
    for r in rows:
        party = format_party(r["receptionist"], r["customer"])
        tm = datetime.fromisoformat(r["time"]).strftime("%H:%M:%S")
        msg.append(f"{tm} | {r['type']} | {party} | {fmt_money(r['amount'])}")

    await update.message.reply_text("\n".join(msg))


async def periods(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute("""
    SELECT DISTINCT period FROM records
    WHERE chat_id=?
    ORDER BY period DESC
    LIMIT 10
    """, (update.effective_chat.id,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("暂无历史周期")
        return

    msg = ["🗂 最近周期"]
    for r in rows:
        msg.append(r["period"])
    await update.message.reply_text("\n".join(msg))


async def setreporttimes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, ctx):
        await update.message.reply_text("只有管理员可以设置指定时间")
        return

    if not ctx.args:
        await update.message.reply_text("用法：/setreporttimes 00:00 04:00")
        return

    times_list = ctx.args
    for t in times_list:
        if not re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", t):
            await update.message.reply_text(f"时间格式错误：{t}")
            return

    chat_id = update.effective_chat.id
    replace_report_times(chat_id, sorted(set(times_list)))
    ensure_scheduled_for_chat(ctx.application, chat_id)
    log_action(chat_id, update.effective_user.id, "set_report_times", " ".join(sorted(set(times_list))))
    await update.message.reply_text("✅ 指定汇总时间已更新：\n" + "\n".join(sorted(set(times_list))))


async def reporttimes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_report_times(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("当前没有设置指定时间")
        return
    await update.message.reply_text("当前指定汇总时间：\n" + "\n".join(rows))


async def setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, ctx):
        await update.message.reply_text("只有管理员可以设置提醒人员")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某个人的消息后再发送 /setalert")
        return

    target = update.message.reply_to_message.from_user
    add_alert_member(update.effective_chat.id, target.id, target.username or "", target.full_name or str(target.id))
    log_action(update.effective_chat.id, update.effective_user.id, "set_alert", target.full_name or str(target.id))
    await update.message.reply_text(f"✅ 已加入提醒名单：{target.full_name}")


async def unsetalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, ctx):
        await update.message.reply_text("只有管理员可以设置提醒人员")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某个人的消息后再发送 /unsetalert")
        return

    target = update.message.reply_to_message.from_user
    remove_alert_member(update.effective_chat.id, target.id)
    log_action(update.effective_chat.id, update.effective_user.id, "unset_alert", target.full_name or str(target.id))
    await update.message.reply_text(f"✅ 已移出提醒名单：{target.full_name}")


async def alertlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_alert_members(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("当前没有提醒人员")
        return

    msg = ["📢 提醒名单"]
    for i, r in enumerate(rows, 1):
        if r["username"]:
            msg.append(f"{i}. {r['full_name']} (@{r['username']})")
        else:
            msg.append(f"{i}. {r['full_name']}")
    await update.message.reply_text("\n".join(msg))


async def resetall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, ctx):
        await update.message.reply_text("只有管理员可以清零")
        return

    conn = db()
    conn.execute("DELETE FROM records WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    conn.close()

    log_action(update.effective_chat.id, update.effective_user.id, "reset", "/resetall")
    await update.message.reply_text("本周期已清零")


# ================================
# 定时
# ================================
async def fixed_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.data["chat_id"]
    msg, _ = summary_text(chat_id, period_key())
    await ctx.bot.send_message(chat_id=chat_id, text=msg)


async def reset_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.data["chat_id"]
    conn = db()
    conn.execute("DELETE FROM records WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
    await ctx.bot.send_message(chat_id=chat_id, text="本周期已清零")


async def custom_report_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.data["chat_id"]
    report_time = ctx.job.data.get("report_time", "")
    mentions = mention_text(chat_id)

    msg, net = summary_text(chat_id, period_key())
    title = f"📢 {report_time} 指定时间汇总"

    text = ""
    if mentions:
        text += mentions + "\n\n"
    text += title + "\n\n" + msg

    if net < 0 and mentions:
        text += "\n\n" + mentions + "\n⚠️ 当前充提差为负数，继续加油！"

    await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


def clear_custom_jobs(app: Application, chat_id: int):
    for job in app.job_queue.jobs():
        if job.name and job.name.startswith(f"custom_report_{chat_id}_"):
            job.schedule_removal()


def schedule_custom_jobs(app: Application, chat_id: int):
    clear_custom_jobs(app, chat_id)
    for t in get_report_times(chat_id):
        hh, mm = t.split(":")
        app.job_queue.run_daily(
            custom_report_job,
            time=time(int(hh), int(mm), tzinfo=TZ),
            days=(0, 1, 2, 3, 4, 5, 6),
            data={"chat_id": chat_id, "report_time": t},
            name=f"custom_report_{chat_id}_{t}",
        )


def ensure_scheduled_for_chat(app: Application, chat_id: int):
    scheduled = app.bot_data.setdefault("scheduled_chat_ids", set())

    if chat_id not in scheduled:
        app.job_queue.run_daily(
            fixed_job,
            time=time(FIXED_SUMMARY.hour, FIXED_SUMMARY.minute, tzinfo=TZ),
            days=(0, 1, 2, 3, 4, 5, 6),
            data={"chat_id": chat_id},
            name=f"fixed_summary_{chat_id}",
        )

        app.job_queue.run_daily(
            reset_job,
            time=time(RESET_TIME.hour, RESET_TIME.minute, tzinfo=TZ),
            days=(0, 1, 2, 3, 4, 5, 6),
            data={"chat_id": chat_id},
            name=f"reset_{chat_id}",
        )

        scheduled.add(chat_id)

    schedule_custom_jobs(app, chat_id)


# ================================
# 启动后
# ================================
async def post_init(app: Application):
    commands = [
        BotCommand("start", "启动机器人"),
        BotCommand("summary", "查看当前汇总"),
        BotCommand("current", "查看当前汇总"),
        BotCommand("details", "查看明细"),
        BotCommand("periods", "查看历史周期"),
        BotCommand("setreporttimes", "设置指定时间"),
        BotCommand("reporttimes", "查看指定时间"),
        BotCommand("setalert", "设置提醒人员（回复某人）"),
        BotCommand("unsetalert", "取消提醒人员（回复某人）"),
        BotCommand("alertlist", "查看提醒人员"),
        BotCommand("resetall", "手动清零"),
        BotCommand("cancel", "退出设置状态"),
    ]
    await app.bot.set_my_commands(commands)

    for chat_id in get_chat_ids():
        ensure_scheduled_for_chat(app, chat_id)


# ================================
# 主程序
# ================================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("current", current))
    app.add_handler(CommandHandler("details", details))
    app.add_handler(CommandHandler("periods", periods))
    app.add_handler(CommandHandler("setreporttimes", setreporttimes))
    app.add_handler(CommandHandler("reporttimes", reporttimes))
    app.add_handler(CommandHandler("setalert", setalert))
    app.add_handler(CommandHandler("unsetalert", unsetalert))
    app.add_handler(CommandHandler("alertlist", alertlist))
    app.add_handler(CommandHandler("resetall", resetall))

    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle))

    logger.info("Enterprise bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
