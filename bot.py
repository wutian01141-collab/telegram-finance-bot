# =========================
# Telegram 出入款统计机器人（最终版）（作者：悟天）
# =========================

import os
import re
import sqlite3
import logging
from zoneinfo import ZoneInfo
from datetime import datetime, time, timedelta

from telegram import Update
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    Defaults,
    filters,
)

# 固定时间
FIXED_SUMMARY = time(10, 0)  # 10:00（不@）
RESET_TIME = time(13, 0)     # 13:00 清零提示

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =========================
# 数据库
# =========================
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    c = db().cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS records (
        chat_id INTEGER,
        type TEXT,
        customer TEXT,
        amount REAL,
        is_result INTEGER,
        time TEXT,
        period TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        chat_id INTEGER,
        user_id INTEGER,
        name TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS report_times (
        chat_id INTEGER,
        t TEXT
    )
    """)

    db().commit()


# =========================
# 工具
# =========================
def now():
    return datetime.now(TZ)


def period_key():
    n = now()
    if n.time() < RESET_TIME:
        return (n - timedelta(days=1)).strftime("%Y-%m-%d")
    return n.strftime("%Y-%m-%d")


def fmt_money(v):
    return f"{v:.2f} USDT"


# =========================
# 解析模板
# =========================
def parse(text):
    if "入金" not in text and "出款" not in text:
        return None

    type_ = "入金" if "入金" in text else "出款"

    amount = re.search(r"金额\s*[:：]\s*([0-9.]+)", text)
    customer = re.search(r"客户\s*[:：]\s*(.+)", text)

    if not amount or not customer:
        return None

    amount = round(float(amount.group(1)), 2)
    customer_raw = customer.group(1).strip()
    customer_name = customer_raw.split("/")[0]

    is_result = 1 if "Result" in text else 0

    return type_, customer_name, amount, is_result


# =========================
# 插入记录
# =========================
def add_record(chat_id, type_, customer, amount, is_result):
    conn = db()
    conn.execute("""
    INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        chat_id,
        type_,
        customer,
        amount,
        is_result,
        now().isoformat(),
        period_key()
    ))
    conn.commit()


# =========================
# 汇总
# =========================
def summary(chat_id, p):
    rows = db().execute("""
    SELECT * FROM records WHERE chat_id=? AND period=?
    """, (chat_id, p)).fetchall()

    ins = [r for r in rows if r["type"] == "入金"]
    outs = [r for r in rows if r["type"] == "出款"]

    total_in = sum(r["amount"] for r in ins)
    total_out = sum(r["amount"] for r in outs)
    net = total_in - total_out
    result_count = sum(r["is_result"] for r in rows)

    def line(r):
        t = datetime.fromisoformat(r["time"]).strftime("%H:%M:%S")
        return f"{t} | {r['customer']} | {fmt_money(r['amount'])}"

    text = []

    text.append("📢 汇总")
    text.append("")
    text.append("💸 出款：")
    text += [line(r) for r in outs] or ["无"]
    text.append(f"\n总出款：-{fmt_money(total_out)}")

    text.append("\n💰 入款：")
    text += [line(r) for r in ins] or ["无"]
    text.append(f"\n总入款：{fmt_money(total_in)}")

    text.append(f"\n📊 充提差：{fmt_money(net)}")
    text.append(f"\n🆕 新单：{result_count}")

    if net < 0:
        text.append("\n⚠️ 截至目前充提差为负数，快点加油！！！！")

    return "\n".join(text)


# =========================
# 自动任务
# =========================
async def fixed_job(ctx):
    chat_id = ctx.job.data
    await ctx.bot.send_message(chat_id, summary(chat_id, period_key()))


async def reset_job(ctx):
    await ctx.bot.send_message(ctx.job.data, "🧹 此周期账单归零，并重新开始计算")


async def custom_job(ctx):
    chat_id = ctx.job.data
    mention = build_mentions(chat_id)
    await ctx.bot.send_message(chat_id, mention + "\n\n" + summary(chat_id, period_key()), parse_mode="HTML")


def build_mentions(chat_id):
    rows = db().execute("SELECT * FROM alerts WHERE chat_id=?", (chat_id,)).fetchall()
    return " ".join(f"<a href='tg://user?id={r['user_id']}'>{r['name']}</a>" for r in rows)


# =========================
# 命令
# =========================
async def start(update: Update, ctx):
    await update.message.reply_text("机器人已启动")


async def setalert(update: Update, ctx):
    if not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    db().execute("INSERT INTO alerts VALUES (?, ?, ?)", (update.effective_chat.id, u.id, u.full_name))
    db().commit()
    await update.message.reply_text("已添加提醒人员")


async def settimes(update: Update, ctx):
    chat_id = update.effective_chat.id
    db().execute("DELETE FROM report_times WHERE chat_id=?", (chat_id,))
    for t in ctx.args:
        db().execute("INSERT INTO report_times VALUES (?, ?)", (chat_id, t))
    db().commit()
    await update.message.reply_text("已设置时间")


# =========================
# 消息处理
# =========================
async def handle(update: Update, ctx):
    text = update.message.text

    parsed = parse(text)
    if parsed:
        add_record(update.effective_chat.id, *parsed)
        await update.message.reply_text("已记录")
        return

    # 客户查询
    rows = db().execute("""
    SELECT DISTINCT customer FROM records WHERE chat_id=? AND period=?
    """, (update.effective_chat.id, period_key())).fetchall()

    names = [r["customer"] for r in rows]
    if text in names:
        rows = db().execute("""
        SELECT * FROM records WHERE chat_id=? AND period=? AND customer=?
        """, (update.effective_chat.id, period_key(), text)).fetchall()

        msg = f"🔎 {text}\n"
        for r in rows:
            msg += f"{r['type']} {fmt_money(r['amount'])}\n"

        await update.message.reply_text(msg)


# =========================
# 主程序
# =========================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setalert", setalert))
    app.add_handler(CommandHandler("setreporttimes", settimes))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # 固定任务
    app.job_queue.run_daily(fixed_job, FIXED_SUMMARY, data=None)
    app.job_queue.run_daily(reset_job, RESET_TIME, data=None)

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
