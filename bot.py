# ================================
# Telegram 出入款统计机器人（最终稳定版）
# 作者：悟天
# 功能：
# 1. 支持纯文字模板 / 图片+配文模板
# 2. 模板不记金额，只记客户和 Result
# 3. 回复模板发送“收到99U / 已出50U”时，继承模板客户名
# 4. 直接发送“收到99U / 已出50U”也可记录（客户默认为未命名客户）
# 5. /summary /details /periods 可用
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

FIXED_SUMMARY = time(10, 0)   # 10:00 自动汇总
RESET_TIME = time(13, 0)      # 13:00 清零提示

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


# ================================
# 工具
# ================================
def now():
    return datetime.now(TZ)


def period_key():
    return now().strftime("%Y-%m-%d")


def fmt_money(x):
    return f"{x:.2f} USDT"


def message_text_content(message) -> str:
    """
    统一读取消息正文：
    - 纯文字消息 -> text
    - 图片+文字消息 -> caption
    """
    if not message:
        return ""
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def add_record(chat_id, record_type, customer, amount, is_result):
    conn = db()
    conn.execute(
        """
        INSERT INTO records (chat_id, type, customer, amount, is_result, time, period)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
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


# ================================
# 模板识别
# ================================
def parse_template(text: str):
    """
    模板消息：
    - 识别类型：入金 / 出款
    - 识别客户
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

    customer_raw = customer_match.group(1).strip()
    customer_raw = customer_raw.splitlines()[0].strip()
    customer = customer_raw.split("/")[0].strip()

    is_result = 1 if "result" in text.lower() else 0

    return {
        "type": record_type,
        "amount": 0.0,  # 模板金额不计入统计
        "customer": customer,
        "is_result": is_result,
        "template_only": True,
    }


def extract_customer_from_reply(update: Update):
    """
    从被回复的模板消息中提取客户名
    支持：
    - 纯文字模板（text）
    - 图片+文字模板（caption）
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

    customer_raw = customer_match.group(1).strip()
    customer_raw = customer_raw.splitlines()[0].strip()
    customer = customer_raw.split("/")[0].strip()

    return customer or None


# ================================
# 金额识别
# ================================
def parse_quick_amount(text: str, customer_name: str | None = None):
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


# ================================
# 汇总
# ================================
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

    # 先识别模板
    data = parse_template(text)

    # 再识别金额消息，并优先继承“回复模板”的客户名
    reply_customer = extract_customer_from_reply(update)
    if not data:
        data = parse_quick_amount(text, customer_name=reply_customer)

    # 如果金额消息没拿到客户名，则用“当前周期最近一条模板记录”兜底
    if data and not data.get("template_only") and data["customer"] == "未命名客户":
        last_template = db().execute(
            """
            SELECT customer FROM records
            WHERE chat_id=? AND period=? AND amount=0
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, period_key()),
        ).fetchone()

        if last_template and last_template["customer"]:
            data["customer"] = last_template["customer"]

    # 入库
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
    await update.message.reply_text("机器人已启动")


async def summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = summary_text(update.effective_chat.id, period_key())
    await update.message.reply_text(text)


async def details(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db().execute(
        """
        SELECT * FROM records
        WHERE chat_id=? AND period=?
        ORDER BY time ASC
        """,
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


# ================================
# 定时
# ================================
async def fixed_job(ctx: ContextTypes.DEFAULT_TYPE):
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
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle))

    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
