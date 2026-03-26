# ================================
# Telegram 出入款统计机器人（悟天）
# 模板不记金额，只记客户和 Result
# 收到/已出金额如果回复模板，则继承模板客户名
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
    """
    模板消息：
    - 识别客户
    - 识别 Result
    - 不识别模板金额，不记入统计
    """
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

    customer_raw = customer_match.group(1).strip()
    customer = customer_raw.split("/")[0].strip()
    is_result = 1 if re.search(r"\bResult\b", text, re.IGNORECASE) else 0

    return {
        "type": record_type,
        "amount": 0.0,  # 模板金额不计入统计
        "customer": customer,
        "is_result": is_result,
        "template_only": True,
    }


def extract_customer_from_reply(update: Update):
    """
    如果金额消息是“回复某条模板消息”发送的，
    则从被回复的模板中提取客户名。
    """
    if not update.message:
        return None

    reply = update.message.reply_to_message
    if not reply or not reply.text:
        return None

    text = reply.text.strip()
    customer_match = re.search(r"客户\s*[:：]\s*(.+)", text)
    if not customer_match:
        return None

    customer_raw = customer_match.group(1).strip()
    customer = customer_raw.split("/")[0].strip()
    return customer


def parse_quick_amount(text: str, customer_name: str | None = None):
    """
    只识别真正的金额消息：
    - 收到99U / 收到 99u
    - 已出71.71u / 出款50U / 出50u
    如果该消息是回复模板，则继承模板里的客户名。
    """
    t = text.strip()

    in_match = re.search(r"收到\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", t)
    if in_match:
        return {
            "type": "入金",
            "amount": round(float(in_match.group(1)), 2),
            "customer": customer_name or "未命名客户",
            "is_result": 0,
            "template_only": False,
        }

    out_match = re.search(r"(?:已出|出款|出)\s*([0-9]+(?:\.[0-9]+)?)\s*[uU]\b", t)
    if out_match:
        return {
            "type": "出款",
            "amount": round(float(out_match.group(1)), 2),
            "customer": customer_name or "未命名客户",
            "is_result": 0,
            "template_only": False,
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

    # 先识别模板
    data = parse_template(text)

    # 如果不是模板，再识别金额消息；金额消息优先继承“被回复模板”的客户名
    reply_customer = extract_customer_from_reply(update)
    if not data:
        data = parse_quick_amount(text, customer_name=reply_customer)

    if data:
        add_record(
            chat_id=chat_id,
            record_type=data["type"],
            customer=data["customer"],
            amount=data["amount"],
            is_result=data["is_result"],
        )

        if data.get("template_only"):
            msg = (
                f"✅ 已识别模板\n"
                f"客户：{data['customer']}\n"
                f"新单：{'是' if data['is_result'] else '否'}\n"
                f"金额不计入统计"
            )
        else:
            msg = (
                f"✅ 已记录\n"
                f"类型：{data['type']}\n"
                f"客户：{data['customer']}\n"
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
    # 如果没有 chat_id，就不执行，避免报错
    if not ctx.job.chat_id:
        return

    chat_id = ctx.job.chat_id
    text = summary_text(chat_id, period_key())
    await ctx.bot.send_message(chat_id=chat_id, text=text)


async def reset_job(ctx: ContextTypes.DEFAULT_TYPE):
    conn = db()
    conn.execute("DELETE FROM records")
    conn.commit()
    conn.close()

    if ctx.job.chat_id:
        await ctx.bot.send_message(
            chat_id=ctx.job.chat_id,
            text="🧹 此周期账单归零，并重新开始计算"
        )


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("details", details))
    app.add_handler(CommandHandler("periods", periods))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
