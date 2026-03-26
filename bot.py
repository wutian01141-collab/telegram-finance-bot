# ================================
# Telegram 出入款统计机器人（悟天）
# ================================

import os
import re
import sqlite3
import logging
from zoneinfo import ZoneInfo
from datetime import datetime, time

from telegram import Update
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

FIXED_SUMMARY = time(10, 0)
RESET_TIME = time(13, 0)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.commit()
    conn.close()


def now():
    return datetime.now(TZ)


def period_key():
    return now().strftime("%Y-%m-%d")


def fmt_money(x):
    return f"{x:.2f} USDT"


def add_record(chat_id, record_type, customer, amount, is_result):
    conn = db()
    conn.execute(
        "INSERT INTO records (chat_id, type, customer, amount, is_result, time, period) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            record_type,
            customer,
            amount,
            is_result,
            now().isoformat(),
            period_key(),
        ),
    )
    conn.commit()
    conn.close()


def parse_template(text: str):
    record_type = None
    if "入金" in text:
        record_type = "入金"
    elif "出款" in text:
        record_type = "出款"
    else:
        return None

    amount_match = re.search(r"金额\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    customer_match = re.search(r"客户\s*[:：]\s*(.+)", text)

    if not amount_match or not customer_match:
        return None

    amount = round(float(amount_match.group(1)), 2)
    customer_raw = customer_match.group(1).strip()
    customer = customer_raw.split("/")[0].strip()
    is_result = 1 if re.search(r"\bResult\b", text, re.IGNORECASE) else 0

    return {
        "type": record_type,
        "amount": amount,
        "customer": customer,
        "is_result": is_result,
    }


def parse_quick_amount(text: str):
    t = text.strip()

    in_match = re.search(r"收到\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", t)
    if in_match:
        return {
            "type": "入金",
            "amount": round(float(in_match.group(1)), 2),
            "customer": "未命名客户",
            "is_result": 0,
        }

    out_match = re.search(r"(?:已出|出款|出)\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", t)
    if out_match:
        return {
            "type": "出款",
            "amount": round(float(out_match.group(1)), 2),
            "customer": "未命名客户",
            "is_result": 0,
        }

    return None


def summary_text(chat_id, p):
    rows = db().execute(
        "SELECT * FROM records WHERE chat_id=? AND period=? ORDER BY time ASC",
        (chat_id, p),
    ).fetchall()

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
    text.append("📊 汇总\n")

    text.append("💸 出款：")
    text += [line(r) for r in outs] or ["无"]
    text.append(f"总出款：-{fmt_money(total_out)}\n")

    text.append("💰 入款：")
    text += [line(r) for r in ins] or ["无"]
    text.append(f"总入款：{fmt_money(total_in)}\n")

    text.append(f"📈 充提差：{fmt_money(net)}")
    text.append(f"🆕 新单：{result_count}")

    return "\n".join(text)


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    data = parse_template(text)
    if not data:
        data = parse_quick_amount(text)

    if data:
        add_record(
            chat_id=chat_id,
            record_type=data["type"],
            customer=data["customer"],
            amount=data["amount"],
            is_result=data["is_result"],
        )
        await update.message.reply_text("✅ 已记录")
        return

    rows = db().execute(
        "SELECT DISTINCT customer FROM records WHERE chat_id=? AND period=?",
        (chat_id, period_key()),
    ).fetchall()
    names = [r["customer"] for r in rows]

    if text in names:
        rows = db().execute(
            "SELECT * FROM records WHERE chat_id=? AND period=? AND customer=?",
            (chat_id, period_key(), text),
        ).fetchall()

        msg = f"📊 {text}\n"
        for r in rows:
            msg += f"{r['type']} {fmt_money(r['amount'])}\n"

        await update.message.reply_text(msg)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("机器人已启动")


async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = summary_text(update.effective_chat.id, period_key())
    await update.message.reply_text(text)


async def details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db().execute(
        "SELECT * FROM records WHERE chat_id=? AND period=? ORDER BY time ASC",
        (update.effective_chat.id, period_key()),
    ).fetchall()

    if not rows:
        await update.message.reply_text("当前周期没有记录")
        return

    msg = "📄 明细\n"
    for r in rows:
        msg += f"{r['type']} {r['customer']} {fmt_money(r['amount'])}\n"

    await update.message.reply_text(msg)


async def periods(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("当前版本先只展示当前周期，请用 /summary 查看")


async def fixed_job(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    text = summary_text(chat_id, period_key())
    await ctx.bot.send_message(chat_id=chat_id, text=text)


async def reset_job(ctx: ContextTypes.DEFAULT_TYPE):
    conn = db()
    conn.execute("DELETE FROM records")
    conn.commit()
    conn.close()
    await ctx.bot.send_message(chat_id=ctx.job.chat_id, text="🧹 此周期账单归零，并重新开始计算")


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("details", details))
    app.add_handler(CommandHandler("periods", periods))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    app.job_queue.run_daily(fixed_job, FIXED_SUMMARY)
    app.job_queue.run_daily(reset_job, RESET_TIME)

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
