# -*- coding: utf-8 -*-
import os
import re
import logging
import sqlite3
from datetime import datetime
import anthropic
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_IDS = set(
    int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "0").split(",") if x.strip()
)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

DB_PATH = "/app/archive.db"

SYSTEM_PROMPT = (
    "Ты -- гроссмейстер и шахматный тренер мирового уровня.\n"
    "Отвечаешь ТОЛЬКО на русском языке, обычным русским текстом.\n"
    "Никакого Markdown: никаких **, ##, *, _, никаких решёток и звёздочек.\n"
    "Разделяй разделы пустой строкой.\n"
    "Название каждого раздела пиши ЗАГЛАВНЫМИ БУКВАМИ на отдельной строке.\n"
    "Используй шахматную нотацию, ходы в квадратных скобках [Nf6].\n"
    "Оценки позиции: +- (белые лучше), -+ (чёрные лучше), = (равно)."
)

ANALYZE_PROMPT = (
    "Разбери партию как строгий тренер. Кратко и по делу.\n\n"
    "{pgn}\n\n"
    "Структура (каждый раздел -- максимум 3-4 предложения):\n\n"
    "ДЕБЮТ\n"
    "Название, главная ошибка, лучший ход.\n\n"
    "МИТТЕЛЬШПИЛЬ\n"
    "2 ключевых момента. Каждый важный ход помечай:\n"
    "[+] [ход] -- сильный ход (почему в 1 предложении)\n"
    "[-] [ход] -- ошибка (что нужно было сыграть)\n"
    "[?] [ход] -- неточность (есть лучше)\n"
    "[!] [ход] -- лучший ход в позиции\n\n"
    "ФИНАЛ\n"
    "Как завершилась партия, ключевой момент с маркировкой.\n\n"
    "УРОКИ\n"
    "3 конкретные вещи что тренировать после этой партии.\n\n"
    "ОЦЕНКА\n"
    "Белые: X/10, Чёрные: X/10. Одно предложение почему.\n\n"
    "Тон: прямо, без пафоса. Ошибки называй ошибками."
)

OPENING_PROMPT = (
    "Разбери этот дебют как шахматный тренер:\n\n"
    "{opening}\n\n"
    "Структура:\n\n"
    "НАЗВАНИЕ\n"
    "Как называется, ECO код.\n\n"
    "ИДЕЯ\n"
    "Главная стратегическая идея за белых и чёрных.\n\n"
    "ОСНОВНЫЕ ВАРИАНТЫ\n"
    "2-3 главные линии с ходами в [скобках].\n\n"
    "ТИПИЧНЫЕ ПЛАНЫ\n"
    "Что делать в миттельшпиле.\n\n"
    "ЛОВУШКИ\n"
    "Главные ошибки новичков.\n\n"
    "СОВЕТ\n"
    "Кому подходит этот дебют по стилю игры."
)


# --- База данных ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            pgn TEXT,
            analysis TEXT,
            short_title TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_to_archive(user_id: int, pgn: str, analysis: str, short_title: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO archive (user_id, pgn, analysis, short_title, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, pgn, analysis, short_title, datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    conn.commit()
    conn.close()


def get_archive(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, short_title, created_at FROM archive WHERE user_id = ? ORDER BY id DESC LIMIT 20",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows


def get_analysis_by_id(record_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT pgn, analysis, short_title, created_at FROM archive WHERE id = ? AND user_id = ?",
        (record_id, user_id)
    ).fetchone()
    conn.close()
    return row


def delete_from_archive(record_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM archive WHERE id = ? AND user_id = ?", (record_id, user_id))
    conn.commit()
    conn.close()


def extract_title(pgn: str, analysis: str) -> str:
    white = re.search(r'\[White "([^"]+)"\]', pgn)
    black = re.search(r'\[Black "([^"]+)"\]', pgn)
    if white and black:
        return f"{white.group(1)} vs {black.group(1)}"
    first_line = analysis.split("\n")[0][:40] if analysis else "Партия"
    return first_line.strip()


def clean_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    return text


# --- Авторизация ---

def is_authorized(update: Update) -> bool:
    if 0 in ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS


def looks_like_pgn(text: str) -> bool:
    has_move_numbers = any(f"{i}." in text for i in range(1, 10))
    has_pgn_tags = "[Event" in text or "[White" in text or "[Black" in text
    return has_move_numbers or has_pgn_tags


# --- Claude ---

async def ask_claude(prompt: str) -> str:
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return clean_markdown(message.content[0].text)
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return f"Ошибка API: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "Что-то пошло не так. Попробуй ещё раз."


# --- Отправка длинных сообщений ---

async def send_long_message(update: Update, text: str, reply_markup=None):
    max_length = 4000
    if len(text) <= max_length:
        await update.message.reply_text(text, reply_markup=reply_markup)
        return
    parts = []
    while text:
        if len(text) <= max_length:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()
    for i, part in enumerate(parts):
        suffix = f"\n\n{i+1}/{len(parts)}" if len(parts) > 1 else ""
        kb = reply_markup if i == len(parts) - 1 else None
        await update.message.reply_text(part + suffix, reply_markup=kb)


# --- Главное меню ---

def main_menu():
    keyboard = [
        [InlineKeyboardButton("Анализ партии", callback_data="menu_analyze")],
        [InlineKeyboardButton("Разбор дебюта", callback_data="menu_opening")],
        [InlineKeyboardButton("Мой архив", callback_data="menu_archive")],
    ]
    return InlineKeyboardMarkup(keyboard)


# --- Хендлеры ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Шахматный тренажёр\n\nВыбери действие или просто скинь партию -- сам разберусь:",
        reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Как пользоваться:\n\n"
        "Анализ партии -- скинь ходы в любом формате:\n"
        "1. e4 e5 2. Nf3 Nc6 3. Bb5...\n\n"
        "Разбор дебюта -- напиши название:\n"
        "Сицилианская защита\n\n"
        "Архив -- все твои разобранные партии\n\n"
        "Можно просто скинуть партию -- бот сам поймёт.",
        reply_markup=main_menu()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        return

    data = query.data

    if data == "menu_analyze":
        context.user_data["mode"] = "analyze"
        await query.message.reply_text(
            "Скинь партию в формате PGN или просто ходами.\n"
            "Пример: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6..."
        )

    elif data == "menu_opening":
        context.user_data["mode"] = "opening"
        await query.message.reply_text(
            "Напиши название дебюта или начальные ходы.\n"
            "Пример: Испанская партия"
        )

    elif data == "menu_archive":
        rows = get_archive(update.effective_user.id)
        if not rows:
            await query.message.reply_text(
                "Архив пуст. Разбери партию -- она сохранится автоматически.",
                reply_markup=main_menu()
            )
            return
        keyboard = []
        for row in rows:
            label = f"{row[2]} -- {row[1][:30]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"view_{row[0]}")])
        keyboard.append([InlineKeyboardButton("Назад", callback_data="menu_main")])
        await query.message.reply_text(
            "Твой архив (последние 20 партий):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("view_"):
        record_id = int(data.split("_")[1])
        row = get_analysis_by_id(record_id, update.effective_user.id)
        if not row:
            await query.message.reply_text("Запись не найдена.")
            return
        pgn, analysis, title, created_at = row
        header = f"{title}\n{created_at}\n\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Удалить", callback_data=f"del_{record_id}")],
            [InlineKeyboardButton("Назад к архиву", callback_data="menu_archive")],
        ])
        text = header + analysis
        if len(text) <= 4000:
            await query.message.reply_text(text, reply_markup=keyboard)
        else:
            await query.message.reply_text(text[:4000])
            await query.message.reply_text(text[4000:], reply_markup=keyboard)

    elif data.startswith("del_"):
        record_id = int(data.split("_")[1])
        delete_from_archive(record_id, update.effective_user.id)
        await query.message.reply_text("Запись удалена.", reply_markup=main_menu())

    elif data == "menu_main":
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text.strip()
    mode = context.user_data.get("mode", "auto")
    user_id = update.effective_user.id

    await update.message.reply_text("Анализирую...")

    is_pgn = False
    if mode == "analyze" or (mode == "auto" and looks_like_pgn(text)):
        prompt = ANALYZE_PROMPT.format(pgn=text)
        context.user_data["mode"] = "auto"
        is_pgn = True
    elif mode == "opening":
        prompt = OPENING_PROMPT.format(opening=text)
        context.user_data["mode"] = "auto"
    else:
        if len(text) < 100 and not looks_like_pgn(text):
            prompt = OPENING_PROMPT.format(opening=text)
        else:
            prompt = ANALYZE_PROMPT.format(pgn=text)
            is_pgn = True

    response = await ask_claude(prompt)

    if is_pgn:
        title = extract_title(text, response)
        save_to_archive(user_id, text, response, title)

    await send_long_message(update, response, reply_markup=main_menu())


async def post_init(application: Application):
    init_db()
    await application.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("help", "Справка"),
    ])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
