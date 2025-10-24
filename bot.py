# bot.py
"""
Telegram raffle bot with Stars payments (choice 1-6).
- Price per participation: 9⭐
- Prize (for owner to pay manually): 25⭐ per winner
- When MAX_PLAYERS participants collected in a group -> bot rolls dice and publishes winners
- Participants MUST have @username (bot records @username)
- Data persisted to players.json, pending.json, config.json, last_round.json

Commands:
- /start (private) - info
- /status (group) - show current count
- /reset (group admin) - clear current round
- /setlimit <n> (group admin) - change limit for this group
- /forcestart (group admin) - force run the round now
- /winners (group) - show winners of last round for this group
"""

import json
import os
import asyncio
import random
import uuid
import logging
from typing import Dict, Any, Optional

from telegram import Update, LabeledPrice
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    PreCheckoutQueryHandler,
)

# ------------------ Logging ------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ Files and default settings ------------------
DATA_FILE = "players.json"
PENDING_FILE = "pending.json"
CONFIG_FILE = "config.json"
LAST_ROUND_FILE = "last_round.json"

# Tokens and configurable defaults come from environment variables (Render / local .env)
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")  # usually empty for Stars
if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set. Please set BOT_TOKEN environment variable.")
    raise SystemExit("BOT_TOKEN is required. Set BOT_TOKEN environment variable and restart.")

# Defaults (can be overridden by config.json or env)
DEFAULT_MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "100"))
PRICE_STARS = int(os.getenv("PRICE_STARS", "9"))
PRIZE_STARS = int(os.getenv("PRIZE_STARS", "25"))
CURRENCY = os.getenv("CURRENCY", "XTR")
# AMOUNT_MULTIPLIER: change to 100 if invoice shows 900 instead of 9
AMOUNT_MULTIPLIER = int(os.getenv("AMOUNT_MULTIPLIER", "1"))

# In-memory structures
players: Dict[str, Dict[str, Dict[str, Any]]] = {}  # {chat_key: {user_key: {"username":..., "choice":...}}}
pending: Dict[str, Dict[str, Any]] = {}            # {payload: {chat_id, user_id, username, choice}}
config: Dict[str, Any] = {}                        # will contain per-chat limits if needed
last_round: Dict[str, Any] = {}                    # store last round results per chat


# ---------- Persistence ----------
def load_json_file(path: str) -> Optional[Dict]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)
            return None
    return None


def save_json_file(path: str, data: Dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to write %s: %s", path, e)


def load_all_data():
    global players, pending, config, last_round
    p = load_json_file(DATA_FILE)
    players = p if isinstance(p, dict) else {}
    pd = load_json_file(PENDING_FILE)
    pending = pd if isinstance(pd, dict) else {}
    cfg = load_json_file(CONFIG_FILE)
    config = cfg if isinstance(cfg, dict) else {}
    lr = load_json_file(LAST_ROUND_FILE)
    last_round = lr if isinstance(lr, dict) else {}


def save_all_data():
    save_json_file(DATA_FILE, players)
    save_json_file(PENDING_FILE, pending)
    save_json_file(CONFIG_FILE, config)
    save_json_file(LAST_ROUND_FILE, last_round)


# ---------- Helpers ----------
def chat_key(chat_id: int) -> str:
    return str(chat_id)


def user_key(user_id: int) -> str:
    return str(user_id)


def get_max_players_for_chat(ck: str) -> int:
    """Return max players for given chat key (may be per-chat in config)."""
    if ck in config and isinstance(config[ck].get("max_players"), int):
        return config[ck]["max_players"]
    return DEFAULT_MAX_PLAYERS


def set_max_players_for_chat(ck: str, new_limit: int):
    """Set per-chat max players in config and persist."""
    if ck not in config:
        config[ck] = {}
    config[ck]["max_players"] = int(new_limit)
    save_json_file(CONFIG_FILE, config)


async def is_user_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# ---------- Commands ----------
async def start_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Привет! 🎲 Чтобы участвовать: в группе отправь число 1–6. "
            "Я пришлю счёт на оплату (9⭐) в личку. После оплаты ты будешь зарегистрирован в текущем раунде."
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Команда /status доступна только в группе.")
        return
    ck = chat_key(update.effective_chat.id)
    cnt = len(players.get(ck, {}))
    maxp = get_max_players_for_chat(ck)
    await update.message.reply_text(f"📋 Зарегистрировано {cnt}/{maxp} участников.")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Команда /reset доступна только в группе.")
        return
    user_id = update.effective_user.id
    ok = await is_user_admin(update.effective_chat.id, user_id, context)
    if not ok:
        await update.message.reply_text("❌ Только администратор может сбросить раунд.")
        return
    ck = chat_key(update.effective_chat.id)
    players.pop(ck, None)
    save_all_data()
    await update.message.reply_text("♻️ Раунд сброшен.")


# ---------- New admin commands ----------
async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setlimit N  - set new limit for this group (admin only)
    /setlimit    - show current limit
    """
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Команда /setlimit доступна только в группе.")
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    ok = await is_user_admin(chat_id, user_id, context)
    if not ok:
        await update.message.reply_text("❌ Только администратор может менять лимит.")
        return

    ck = chat_key(chat_id)
    args = context.args or []
    if not args:
        cur = get_max_players_for_chat(ck)
        await update.message.reply_text(f"Текущий лимит для этой группы: {cur} участников.")
        return

    # try parse
    try:
        n = int(args[0])
        if n <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Укажите корректное положительное число. Пример: /setlimit 50")
        return

    set_max_players_for_chat(ck, n)
    await update.message.reply_text(f"✅ Лимит для этой группы изменён на {n} участников.")


async def forcestart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /forcestart - admin only, triggers execute_round now for current group
    """
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Команда /forcestart работает только в группе.")
        return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    ok = await is_user_admin(chat_id, user_id, context)
    if not ok:
        await update.message.reply_text("❌ Только администратор может принудительно запустить розыгрыш.")
        return

    ck = chat_key(chat_id)
    cnt = len(players.get(ck, {}))
    await update.message.reply_text(f"⚠️ Инициирован принудительный розыгрыш. Сейчас участников: {cnt}.")
    asyncio.create_task(execute_round(chat_id, context))


async def winners_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /winners - show last round winners for this group
    """
    ck = chat_key(update.effective_chat.id) if update.effective_chat else None
    if not ck:
        await update.message.reply_text("Не удалось определить чат.")
        return
    data = last_round.get(ck)
    if not data:
        await update.message.reply_text("Информация о последнем раунде отсутствует.")
        return
    result = data.get("result")
    winners = data.get("winners", [])
    if winners:
        text = f"🎲 Последний раунд — выпало {result}.\n🏆 Победители:\n" + "\n".join(winners)
    else:
        text = f"🎲 Последний раунд — выпало {result}.\n😅 Никто не угадал."
    await update.message.reply_text(text)


# ---------- Group message handler ----------
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return
    choice = int(text)
    if choice < 1 or choice > 6:
        return

    user = update.effective_user
    if not user.username:
        await update.message.reply_text(f"❗ {user.first_name}, установи @username, чтобы участвовать.")
        return

    ck = chat_key(update.effective_chat.id)
    uk = user_key(user.id)
    if ck not in players:
        players[ck] = {}
    if uk in players[ck]:
        await update.message.reply_text(f"ℹ️ {user.first_name}, ты уже участвуешь.")
        return

    payload = f"participation:{uuid.uuid4().hex}"
    pending[payload] = {
        "chat_id": ck,
        "user_id": uk,
        "username": "@" + user.username,
        "choice": choice,
    }
    save_all_data()

    try:
        prices = [LabeledPrice(label=f"Выбор {choice}", amount=PRICE_STARS * AMOUNT_MULTIPLIER)]
        await context.bot.send_invoice(
            chat_id=user.id,
            title="Участие в розыгрыше",
            description=f"Оплата участия ({PRICE_STARS}⭐). Выбор {choice}.",
            payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
        )
        await update.message.reply_text(
            f"✅ {user.mention_html()}, счёт на оплату отправлен в личные сообщения.", parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Failed to send invoice to user %s: %s", user.id, e)
        await update.message.reply_text(
            f"❗ Не удалось отправить счёт. {user.first_name}, напиши боту в личку и нажми /start."
        )
        pending.pop(payload, None)
        save_all_data()


# ---------- Payments ----------
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query:
        await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    s = msg.successful_payment
    payload = s.invoice_payload if s else None

    if not payload or payload not in pending:
        await msg.reply_text("Спасибо за оплату! Но я не нашёл связанного раунда.")
        return

    record = pending.pop(payload)
    save_all_data()

    ck = record["chat_id"]
    uk = record["user_id"]
    uname = record["username"]
    choice = record["choice"]

    if ck not in players:
        players[ck] = {}
    players[ck][uk] = {"username": uname, "choice": choice}
    save_all_data()

    await msg.reply_text(f"✅ Оплата принята! Ты зарегистрирован (число {choice}).")

    try:
        chat_id = int(ck)
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {uname} зарегистрирован! ({len(players[ck])}/{get_max_players_for_chat(ck)})")
    except Exception:
        pass

    if len(players[ck]) >= get_max_players_for_chat(ck):
        asyncio.create_task(execute_round(int(ck), context))


# ---------- Execute round ----------
async def execute_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    ck = chat_key(chat_id)
    try:
        msg = await context.bot.send_dice(chat_id=chat_id)
        result = msg.dice.value if getattr(msg, "dice", None) and getattr(msg.dice, "value", None) else random.randint(1, 6)
        winners = [info["username"] for info in players.get(ck, {}).values() if info["choice"] == result]

        text = f"🎲 Выпало *{result}*!\n\n"
        if winners:
            text += "🏆 Победители:\n" + "\n".join(winners) + f"\n\n💰 Владелец выплатит по {PRIZE_STARS}⭐ каждому."
        else:
            text += "😅 Никто не угадал."

        await context.bot.send_message(chat_id, text, parse_mode="Markdown")

        # Save last round info for this chat
        last_round[ck] = {"result": result, "winners": winners}
        save_json_file(LAST_ROUND_FILE, last_round)

    except Exception as e:
        logger.exception("Error during execute_round: %s", e)
        try:
            await context.bot.send_message(chat_id, f"⚠️ Ошибка розыгрыша: {e}")
        except Exception:
            pass
    finally:
        # clear players for that chat and persist
        players.pop(ck, None)
        save_all_data()


# ---------- Main ----------
def main():
    load_all_data()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Basic commands
    app.add_handler(CommandHandler("start", start_private))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    # New admin commands
    app.add_handler(CommandHandler("setlimit", setlimit_cmd))
    app.add_handler(CommandHandler("forcestart", forcestart_cmd))
    app.add_handler(CommandHandler("winners", winners_cmd))

    # Payment handlers
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Group messages (numbers)
    group_filter = filters.ChatType.GROUP | filters.ChatType.SUPERGROUP
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & group_filter, handle_group_message))

    logger.info("✅ Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
