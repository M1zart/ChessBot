# -*- coding: utf-8 -*-
import os
import logging
import anthropic
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """Ty -- grossmeyster i shakhmatnyy trener mirovogo urovnya.
Otvechaesh tolko na russkom yazyke.
Analiziruesh partii i debyuty kak professionalnyy trener -- konkretno, bez vody.
Ispolzuesh shakhmatnuyu natatsiyu, ukazyvaesh konkretnyye khody v kvadratnykh skobkakh [Nf6].
Otsenki pozitsii: +- (belye luchshe), -+ (chyornye luchshe), = (ravno).
Telegram ne podderzhivayet LaTeX -- ispolzuy tolko obychnyy tekst."""

ANALYZE_PROMPT = """Razberi partiyu kak strogiy trener. Kratko i po delu -- eto Telegram, ne kniga.

{pgn}

Struktura (kazhdyy razdel -- maksimum 3-4 predlozheniya):

1. DEBYUT -- nazvaniye, glavnaya oshibka, luchshiy khod

2. MITTELSHPIL -- 2 klyuchevykh momenta. Kazhdyy vazhdnyy khod pomechay:
   [+] [khod] -- silnyy khod (pochemu v 1 predlozhenii)
   [-] [khod] -- oshibka (chto nuzhno bylo sygrat)
   [?] [khod] -- netochnost (yest luchshe)
   [!] [khod] -- luchshiy khod v pozitsii

3. FINAL -- kak zavershilas partiya, klyuchevoy moment s markirovkoy

4. UROKI -- 3 konkretnyye veshchi chto trenirovat posle etoy partii

5. OTSENKA -- belye X/10, chyornyye X/10, odno predlozheniye pochemu

Ton: pryamo, bez pafosa. Oshibki nazyvay oshibkami. Glavnaya tsel -- nauchit."""

OPENING_PROMPT = """Razberi etot debyut kak shakhmatnyy trener:

{opening}

Struktura:
1. NAZVANIYE I ISTORIYA -- kak nazyvayetsya, ECO kod
2. IDEYA -- glavnaya strategicheskaya ideya za belykh i chyornykh
3. OSNOVNYYE VARIANTY -- 2-3 glavnye linii s khodami v [skobkakh]
4. TIPICHNYYE PLANY -- chto delat v mittelshpile
5. LOVUSHKI -- glavnyye takticheskiye ugrozy i oshibki novichkov
6. SOVET -- komu podkhodit etot debyut po stilyu igry

Pishi zhivo, kak trener."""


def is_authorized(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID


def looks_like_pgn(text: str) -> bool:
    text = text.strip()
    has_move_numbers = any(f"{i}." in text for i in range(1, 10))
    has_pgn_tags = "[Event" in text or "[White" in text or "[Black" in text
    return has_move_numbers or has_pgn_tags


async def ask_claude(prompt: str) -> str:
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return f"Oshibka API: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "Chto-to poshlo ne tak. Poprobuy eshchyo raz."


async def send_long_message(update: Update, text: str):
    max_length = 4000
    if len(text) <= max_length:
        await update.message.reply_text(text)
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
        await update.message.reply_text(part + suffix)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (
        "Shakhmatnyy trenazhyor\n\n"
        "Umeyu:\n"
        "- Analizirovat partii -- skin PGN ili prosto khody\n"
        "- Razbirat debyuty -- nazvaniye ili nachalnyye khody\n\n"
        "Komandy:\n"
        "/analyze -- razbor partii\n"
        "/opening -- razbor debyuta\n"
        "/help -- spravka\n\n"
        "Ili prosto skin partiyu -- sam opredelu chto eto."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (
        "Kak polzovatsya:\n\n"
        "Analiz partii:\n"
        "Prosto skin khody:\n"
        "1. e4 e5 2. Nf3 Nc6 3. Bb5...\n\n"
        "Razbor debyuta:\n"
        "Napishi /opening i zatom:\n"
        "- nazvaniye: Sitsilianskaya zashchita\n"
        "- ili khody: 1. e4 c5 2. Nf3 d6\n\n"
        "Mozhno prosto skinut partiyu bez komandy -- bot sam poymet."
    )
    await update.message.reply_text(text)


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    context.user_data["mode"] = "analyze"
    await update.message.reply_text(
        "Skin partiyu v formate PGN ili prosto khodami.\n"
        "Primer: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6..."
    )


async def opening_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    context.user_data["mode"] = "opening"
    await update.message.reply_text(
        "Napishi nazvaniye debyuta ili nachalnyye khody.\n"
        "Primer: Ispanskaya partiya ili 1. e4 e5 2. Nf3 Nc6 3. Bb5"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text.strip()
    mode = context.user_data.get("mode", "auto")

    await update.message.reply_text("Analiziruyu...")

    if mode == "analyze" or (mode == "auto" and looks_like_pgn(text)):
        prompt = ANALYZE_PROMPT.format(pgn=text)
        context.user_data["mode"] = "auto"
    elif mode == "opening":
        prompt = OPENING_PROMPT.format(opening=text)
        context.user_data["mode"] = "auto"
    else:
        if len(text) < 100 and not looks_like_pgn(text):
            prompt = OPENING_PROMPT.format(opening=text)
        else:
            prompt = ANALYZE_PROMPT.format(pgn=text)

    response = await ask_claude(prompt)
    await send_long_message(update, response)


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Nachalo raboty"),
        BotCommand("analyze", "Razbor partii"),
        BotCommand("opening", "Razbor debyuta"),
        BotCommand("help", "Spravka"),
    ])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("opening", opening_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot zapushchen...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
