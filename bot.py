# ================================
# Telegram 出入款统计机器人（最终稳定版）
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

# ================================
# 配置
# ================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("请设置 BOT_TOKEN")

DB_FILE = "data.db"
TZ = ZoneInfo("Asia/Bangkok")

FIXED_SUMMARY = time(10, 0)
RESET_TIME = time(13, 0)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================================
# 数据库
# ================================

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
    db().commit()

# ================================
# 工具
# ================================

def now():
    return datetime.now(TZ)

def period_key():
    return now().strftime("%Y-%m-%d")

def fmt_money(x):
    return f"{x:.2f} USDT"

# ================================
# 解析消息
# ================================

def parse(text):
    t = "入金" if "入金" in text else "出款" if "出款" in text else None
    if not t:
        return None

    amount_match = re.search(r"金额\s*[:：]\s*([\d.]+)", text)
    customer_match = re.search(r"客户\s*[:：]\s*(\S+)", text)

    if not amount_match or not customer_match:
        return None

    amount = float(amount_match.group(1))
    customer = customer_match.group(1).split("/")[0]

    is_result = 1 if "Result" in text else 0

    return {
        "type": t,
        "amount": amount,
        "customer": customer,
        "is_result": is_result,
    }

# ================================
# 汇总
# ================================

def summary_text(chat_id, p):
    rows = db().execute(
        "SELECT * FROM records WHERE chat_id=? AND period=?",
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

# ================================
# 处理消息
# ================================

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    data = parse(text)
    if data:
        db().execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
            (
                chat_id,
                data["type"],
                data["customer"],
                data["amount"],
                data["is_result"],
                now().isoformat(),
                period_key(),
            ),
        )
        db().commit()
        await update.message.reply_text("✅ 已记录")
        return

    # 查询客户
    rows = db().execute(
        "SELECT DISTINCT customer FROM records WHERE chat_id=?",
        (chat_id,),
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

# ================================
# 命令
# ================================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("机器人已启动")

async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = summary_text(update.effective_chat.id, period_key())
    await update.message.reply_text(text)

async def details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db().execute(
        "SELECT * FROM records WHERE chat_id=? AND period=?",
        (update.effective_chat.id, period_key()),
    ).fetchall()

    msg = "📄 明细\n"
    for r in rows:
        msg += f"{r['type']} {r['customer']} {fmt_money(r['amount'])}\n"

    await update.message.reply_text(msg)

async def periods(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("仅当前周期")

# ================================
# 定时
# ================================

async def fixed_job(ctx):
    chat_id = ctx.job.chat_id
    text = summary_text(chat_id, period_key())
    await ctx.bot.send_message(chat_id, text)

async def reset_job(ctx):
    db().execute("DELETE FROM records")
    db().commit()
    await ctx.bot.send_message(ctx.job.chat_id, "此周期账单归零")

# ================================
# 主程序
# ================================

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
