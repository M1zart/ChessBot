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

SYSTEM_PROMPT = """Ty -- grossmeyster i shakhmatnyy trener mirovogo urovnya.
Otvechaesh tolko na russkom yazyke.
Analiziruesh partii i debyuty kak professionalnyy trener -- konkretno, bez vody.
Ispolzuesh shakhmatnuyu notaciyu, ukazyvaesh konkretnyye khody v kvadratnykh skobkakh [Nf6].
Otsenki pozicii: +- (belye luchshe), -+ (chyornye luchshe), = (ravno).
VAZNO: Ne ispolzuy Markdown -- nikakikh **, ##, *, _. Tolko obychnyy tekst."""

ANALYZE_PROMPT = """Razberi partiyu kak strogiy trener. Kratko i po delu.

{pgn}

Struktura (kazhdyy razdel -- maksimum 3-4 predlozheniya):

1. DEBYUT -- nazvaniye, glavnaya oshibka, luchshiy khod

2. MITTELSHPIL -- 2 klyuchevykh momenta s markirovkoy khodov:
   [+] [khod] -- silnyy khod (pochemu v 1 predlozhenii)
   [-] [khod] -- oshibka (chto nuzhno bylo sygrat)
   [?] [khod] -- netochnost (yest luchshe)
   [!] [khod] -- luchshiy khod v pozicii

3. FINAL -- kak zavershilas partiya, klyuchevoy moment

4. UROKI -- 3 konkretnyye veshchi chto trenirovat

5. OTSENKA -- Belye: X/10, Chyornye: X/10. Odno predlozheniye pochemu.

Ton: pryamo, bez pafosa. Oshibki nazyvay oshibkami."""

OPENING_PROMPT = """Razberi etot debyut kak shakhmatnyy trener:

{opening}

Struktura:
1. NAZVANIYE -- kak nazyvayetsya, ECO kod
2. IDEYA -- glavnaya strategicheskaya ideya za belykh i chyornykh
3. OSNOVNYYE VARIANTY -- 2-3 glavnye linii s khodami v [skobkakh]
4. TIPICHNYYE PLANY -- chto delat v mittelshpile
5. LOVUSHKI -- glavnyye oshibki novichkov
6. SOVET -- komu podkhodit etot debyut

Ne ispolzuy Markdown. Tolko obychnyy tekst."""


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
    first_line = analysis.split("\n")[0][:40] if analysis else "Partiya"
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
        return f"Oshibka API: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "Chto-to poshlo ne tak. Poprobuy eshchyo raz."


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
        [InlineKeyboardButton("Analiz partii", callback_data="menu_analyze")],
        [InlineKeyboardButton("Razbor debyuta", callback_data="menu_opening")],
        [InlineKeyboardButton("Moy arkhiv", callback_data="menu_archive")],
    ]
    return InlineKeyboardMarkup(keyboard)


# --- Хендлеры ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Shakhmatnyy trenazhyor\n\n"
        "Vyberi deystviye ili prosto skin partiyu -- sam razberus:",
        reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Kak polzovatsya:\n\n"
        "Analiz partii -- skin khody v lyubom formate:\n"
        "1. e4 e5 2. Nf3 Nc6 3. Bb5...\n\n"
        "Razbor debyuta -- napishi nazvaniye:\n"
        "Sitsilianskaya zashchita\n\n"
        "Arkhiv -- vse tvoi razobrannyye partii\n\n"
        "Mozhno prosto skinut partiyu -- bot sam poymet.",
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
            "Skin partiyu v formate PGN ili prosto khodami.\n"
            "Primer: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6..."
        )

    elif data == "menu_opening":
        context.user_data["mode"] = "opening"
        await query.message.reply_text(
            "Napishi nazvaniye debyuta ili nachalnyye khody.\n"
            "Primer: Ispanskaya partiya"
        )

    elif data == "menu_archive":
        rows = get_archive(update.effective_user.id)
        if not rows:
            await query.message.reply_text(
                "Arkhiv pust. Razberite partiyu -- ona sokhranitsya avtomaticheski.",
                reply_markup=main_menu()
            )
            return
        keyboard = []
        for row in rows:
            label = f"{row[2]} -- {row[1][:30]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"view_{row[0]}")])
        keyboard.append([InlineKeyboardButton("Nazad", callback_data="menu_main")])
        await query.message.reply_text(
            "Tvoy arkhiv (poslednie 20 partiy):",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("view_"):
        record_id = int(data.split("_")[1])
        row = get_analysis_by_id(record_id, update.effective_user.id)
        if not row:
            await query.message.reply_text("Zapis ne naydena.")
            return
        pgn, analysis, title, created_at = row
        header = f"{title}\n{created_at}\n\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Udalit", callback_data=f"del_{record_id}")],
            [InlineKeyboardButton("Nazad k arkhivu", callback_data="menu_archive")],
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
        await query.message.reply_text(
            "Zapis udalena.",
            reply_markup=main_menu()
        )

    elif data == "menu_main":
        await query.message.reply_text(
            "Glavnoye menyu:",
            reply_markup=main_menu()
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text.strip()
    mode = context.user_data.get("mode", "auto")
    user_id = update.effective_user.id

    await update.message.reply_text("Analiziruyu...")

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
        BotCommand("start", "Glavnoye menyu"),
        BotCommand("help", "Spravka"),
    ])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot zapushchen...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
