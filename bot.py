# ================================
# Telegram 出入款统计机器人（最终企业版）
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
RESET_TIME = time(12, 0)      # 12:00 清零并提示

logging.basicConfig(level=logging.INFO)
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
        amount REAL,
        is_result INTEGER,
        time TEXT,
        period TEXT
    )
    """)

    # 兼容旧表结构
    ensure_column(conn, "records", "receptionist", "receptionist TEXT DEFAULT ''")

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

    conn.commit()
    conn.close()


# ================================
# 工具
# ================================
def now():
    return datetime.now(TZ)


def period_key(dt=None):
    dt = dt or now()
    # 每天 12:00 清零，所以 12:00 前算前一天周期
    if dt.time() < RESET_TIME:
        base = dt.date() - timedelta(days=1)
    else:
        base = dt.date()
    return base.strftime("%Y-%m-%d")


def fmt_money(x):
    return f"{x:.2f} USDT"


def period_range_text(p: str):
    d = datetime.strptime(p, "%Y-%m-%d").date()
    next_d = d + timedelta(days=1)
    return f"{d.strftime('%Y-%m-%d')} 21:00 ~ {next_d.strftime('%Y-%m-%d')} 11:00 (TH)"


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


def add_record(chat_id, record_type, customer, receptionist, amount, is_result):
    conn = db()
    conn.execute(
        """
        INSERT INTO records (chat_id, type, customer, receptionist, amount, is_result, time, period)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    conn.commit()
    conn.close()


# ================================
# 模板识别
# ================================
def parse_template(text: str):
    """
    模板：
    - 识别入金/出款
    - 识别客户
    - 识别接待人姓名
    - 识别 Result -> 新单
    - 模板金额不计入统计
    """
    if not text:
        return None

    record_type = None
    if "入金" in text:
        record_type = "入金"
    elif "出款" in text:
        record_type = "出款"
    else:
        return None

    customer_match = re.search(r"客户\s*[:：]\s*(.+)", text)
    if not customer_match:
        return None

    customer_raw = customer_match.group(1).strip().splitlines()[0].strip()
    customer = customer_raw.split("/")[0].strip()

    receptionist_match = re.search(r"接待人姓名\s*[:：]\s*(.+)", text)
    receptionist = ""
    if receptionist_match:
        receptionist = receptionist_match.group(1).strip().splitlines()[0].strip()

    is_result = 1 if "result" in text.lower() else 0

    return {
        "type": record_type,
        "customer": customer,
        "receptionist": receptionist,
        "amount": 0.0,   # 模板金额不计入统计
        "is_result": is_result,
        "template_only": True,
    }


def extract_template_info_from_reply(update: Update):
    """
    从被回复的模板消息中提取：
    - customer
    - receptionist
    支持 text / caption
    """
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
    """
    只识别真正计入统计的金额消息：
    - 收到99U / 收到 99u
    - 已出71.71u / 出款50U / 出50u
    """
    if not text:
        return None

    t = text.strip()

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
# 汇总
# ================================
def summary_text(chat_id, p):
    rows = db().execute(
        "SELECT * FROM records WHERE chat_id=? AND period=? ORDER BY time ASC",
        (chat_id, p),
    ).fetchall()

    # 只展示真正有金额的记录
    money_rows = [r for r in rows if float(r["amount"] or 0) > 0]

    ins = [r for r in money_rows if r["type"] == "入金"]
    outs = [r for r in money_rows if r["type"] == "出款"]

    total_in = sum(r["amount"] for r in ins)
    total_out = sum(r["amount"] for r in outs)
    net = total_in - total_out

    # 新单统计所有记录
    result_count = sum(r["is_result"] for r in rows)

    def line(r):
        t = datetime.fromisoformat(r["time"]).strftime("%H:%M:%S")
        party = format_party(r["receptionist"], r["customer"])
        return f"{t} | {party} | {fmt_money(r['amount'])}"

    text = []
    text.append("📊 汇总\n")
    text.append(f"📅 周期：{period_range_text(p)}\n")

    # 入款放上面
    text.append("💰 入款：")
    text += [line(r) for r in ins] or ["无"]
    text.append(f"总入款：{fmt_money(total_in)}\n")

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
            await update.message.reply_text("请直接发送时间，例如：00:00 04:00 08:30")
        else:
            await update.message.reply_text("只有管理员可以设置指定时间")
        return

    if text == "清零":
        if await is_group_admin(update, ctx):
            conn = db()
            conn.execute("DELETE FROM records WHERE chat_id=?", (chat_id,))
            conn.commit()
            conn.close()
            await update.message.reply_text("本周期已清零")
        else:
            await update.message.reply_text("只有管理员可以清零")
        return

    # 如果正在等待输入指定时间
    if ctx.user_data.get("awaiting_report_times"):
        if not await is_group_admin(update, ctx):
            ctx.user_data["awaiting_report_times"] = False
            await update.message.reply_text("只有管理员可以设置指定时间")
            return

        times_list = text.split()
        for t in times_list:
            if not re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", t):
                await update.message.reply_text("时间格式错误，请用 HH:MM，例如：00:00 04:00")
                return

        replace_report_times(chat_id, sorted(set(times_list)))
        ensure_scheduled_for_chat(ctx.application, chat_id)
        ctx.user_data["awaiting_report_times"] = False
        await update.message.reply_text("✅ 指定汇总时间已更新：\n" + "\n".join(sorted(set(times_list))))
        return

    # 先识别模板
    data = parse_template(text)

    # 再识别金额消息，并优先从“回复模板”里继承客户/接待
    reply_info = extract_template_info_from_reply(update)
    if not data:
        data = parse_quick_amount(
            text,
            customer_name=(reply_info["customer"] if reply_info else None),
            receptionist=(reply_info["receptionist"] if reply_info else None),
        )

    # 如果金额消息没拿到客户名，则用“当前周期最近一条模板记录”兜底
    if data and not data.get("template_only") and data["customer"] == "未命名客户":
        last_template = db().execute(
            """
            SELECT customer, receptionist FROM records
            WHERE chat_id=? AND period=? AND amount=0
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, period_key()),
        ).fetchone()

        if last_template and last_template["customer"]:
            data["customer"] = last_template["customer"]
            data["receptionist"] = last_template["receptionist"] or ""

    # 入库
    if data:
        add_record(
            chat_id=chat_id,
            record_type=data["type"],
            customer=data["customer"],
            receptionist=data.get("receptionist", ""),
            amount=data["amount"],
            is_result=data["is_result"],
        )

        if data.get("template_only"):
            msg = (
                f"✅ 已识别模板\n"
                f"接待/客户：{format_party(data.get('receptionist', ''), data['customer'])}\n"
                f"新单：{'是' if data['is_result'] else '否'}\n"
                f"金额不计入统计"
            )
        else:
            msg = (
                f"✅ 已记录\n"
                f"类型：{data['type']}\n"
                f"接待/客户：{format_party(data.get('receptionist', ''), data['customer'])}\n"
                f"金额：{fmt_money(data['amount'])}"
            )

        await update.message.reply_text(msg)
        return

    # 直接输入客户名，查询当前周期
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

        msg = f"📊 {text}\n"
        for r in rows:
            msg += f"{r['type']} {fmt_money(r['amount'])}\n"

        await update.message.reply_text(msg)


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
        "支持：图片+文字模板、回复模板继承客户、只统计收到/已出金额"
    )

    if await is_group_admin(update, ctx):
        await update.message.reply_text(text, reply_markup=admin_keyboard())
    else:
        await update.message.reply_text(text)


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

    msg = "📄 明细\n"
    for r in rows:
        party = format_party(r["receptionist"], r["customer"])
        msg += f"{r['type']} {party} {fmt_money(r['amount'])}\n"

    await update.message.reply_text(msg)


async def periods(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("当前版本先展示当前周期，请用 /summary 查看")


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
    await update.message.reply_text(f"✅ 已移出提醒名单：{target.full_name}")


async def alertlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_alert_members(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("当前没有提醒人员")
        return

    msg = "📢 提醒名单\n"
    for i, r in enumerate(rows, 1):
        if r["username"]:
            msg += f"{i}. {r['full_name']} (@{r['username']})\n"
        else:
            msg += f"{i}. {r['full_name']}\n"
    await update.message.reply_text(msg)


async def resetall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_group_admin(update, ctx):
        await update.message.reply_text("只有管理员可以清零")
        return

    conn = db()
    conn.execute("DELETE FROM records WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    conn.close()
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

    # 如果充提差为负数，再额外@并提醒加油
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
        BotCommand("periods", "查看周期"),
        BotCommand("setreporttimes", "设置指定时间"),
        BotCommand("reporttimes", "查看指定时间"),
        BotCommand("setalert", "设置提醒人员（回复某人）"),
        BotCommand("unsetalert", "移除提醒人员（回复某人）"),
        BotCommand("alertlist", "查看提醒人员"),
        BotCommand("resetall", "手动清零"),
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

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
