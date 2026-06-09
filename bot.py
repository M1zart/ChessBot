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
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """Ты — гроссмейстер и шахматный тренер мирового уровня. 
Отвечаешь только на русском языке. 
Анализируешь партии и дебюты как профессиональный тренер — конкретно, без воды.
Используешь шахматную нотацию, указываешь конкретные ходы в квадратных скобках [Nf6].
Оценки позиции: ± (белые лучше), ∓ (чёрные лучше), = (равно), +- (белые выигрывают), -+ (чёрные выигрывают).
Telegram не поддерживает LaTeX — используй только обычный текст и эмодзи."""

ANALYZE_PROMPT = """Проведи полный гроссмейстерский разбор этой партии. 
Я — игрок который прислал партию, но неизвестно за какой цвет я играл.
Не делай предположений кто я — просто разбирай объективно обе стороны.
Тон: как строгий тренер, без пафоса и дифирамбов. Указывай ошибки прямо.

{pgn}

Структура разбора:
1. 📖 ДЕБЮТ — название, оценка первых ходов, отступления от теории
2. ⚔️ МИТТЕЛЬШПИЛЬ — 2-4 ключевых момента, ошибки и лучшие альтернативы
3. 🏁 ФИНАЛ — техника реализации или упущенные шансы
4. 💡 ВЫВОДЫ — одна сильная и одна слабая сторона каждого игрока

Пиши живо, как настоящий тренер. Конкретные ходы в [скобках].

5. 💡 СОВЕТЫ — 3 конкретных совета что тренировать по итогам этой партии (дебют, тактика, эндшпиль)
6. 📊 ОЦЕНКА — выставь оценку игры белых и чёрных по 10-балльной шкале с объяснением."""

OPENING_PROMPT = """Разбери этот дебют как шахматный тренер:

{opening}

Структура:
1. 📖 НАЗВАНИЕ И ИСТОРИЯ — как называется, кто играл, ECO код
2. 🎯 ИДЕЯ — главная стратегическая идея за белых и чёрных
3. 📋 ОСНОВНЫЕ ВАРИАНТЫ — 2-3 главные линии с ходами
4. ⚡ ТИПИЧНЫЕ ПЛАНЫ — что делать в миттельшпиле
5. ⚠️ ЛОВУШКИ — главные тактические угрозы и ошибки новичков
6. 💡 СОВЕТ — кому подходит этот дебют по стилю игры

Конкретные ходы в [скобках]. Пиши как тренер, не как энциклопедия."""


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
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return f"❌ Ошибка API: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "❌ Что-то пошло не так. Попробуй ещё раз."


async def send_long_message(update: Update, text: str):
    """Разбивает длинные сообщения на части (лимит Telegram — 4096 символов)."""
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
        suffix = f"\n\n_{i+1}/{len(parts)}_" if len(parts) > 1 else ""
        await update.message.reply_text(part + suffix)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (
        "♟️ *Шахматный тренажёр*\n\n"
        "Я — твой персональный гроссмейстер. Умею:\n\n"
        "📋 *Анализировать партии* — скинь PGN или просто ходы\n"
        "📖 *Разбирать дебюты* — название или начальные ходы\n\n"
        "Команды:\n"
        "/analyze — разобрать партию (потом скинь PGN)\n"
        "/opening — разобрать дебют (потом напиши название или ходы)\n"
        "/help — справка\n\n"
        "Или просто скинь партию — сам определю что это."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (
        "📚 *Как пользоваться:*\n\n"
        "*Анализ партии:*\n"
        "Просто скинь ходы:\n"
        "`1. e4 e5 2. Nf3 Nc6 3. Bb5...`\n\n"
        "Или полный PGN с тегами:\n"
        "`[White \"Kasparov\"]`\n"
        "`[Black \"Karpov\"]`\n"
        "`1. d4 d5 2. c4...`\n\n"
        "*Разбор дебюта:*\n"
        "Напиши `/opening` и затем:\n"
        "— название: `Сицилианская защита`\n"
        "— или ходы: `1. e4 c5 2. Nf3 d6`\n\n"
        "*Совет:* Можно просто скинуть партию без команды — бот сам поймёт."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    context.user_data["mode"] = "analyze"
    await update.message.reply_text(
        "📋 Скинь партию в формате PGN или просто ходами.\n"
        "Например: `1. e4 e5 2. Nf3 Nc6 3. Bb5 a6...`",
        parse_mode="Markdown",
    )


async def opening_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    context.user_data["mode"] = "opening"
    await update.message.reply_text(
        "📖 Напиши название дебюта или начальные ходы.\n"
        "Например: `Испанская партия` или `1. e4 e5 2. Nf3 Nc6 3. Bb5`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = update.message.text.strip()
    mode = context.user_data.get("mode", "auto")

    await update.message.reply_text("🧠 Анализирую...")

    if mode == "analyze" or (mode == "auto" and looks_like_pgn(text)):
        prompt = ANALYZE_PROMPT.format(pgn=text)
        context.user_data["mode"] = "auto"
    elif mode == "opening":
        prompt = OPENING_PROMPT.format(opening=text)
        context.user_data["mode"] = "auto"
    else:
        # Авто-режим: короткий текст без ходов = вопрос о дебюте
        if len(text) < 100 and not looks_like_pgn(text):
            prompt = OPENING_PROMPT.format(opening=text)
        else:
            prompt = ANALYZE_PROMPT.format(pgn=text)

    response = await ask_claude(prompt)
    await send_long_message(update, response)


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Начало работы"),
        BotCommand("analyze", "Разобрать партию"),
        BotCommand("opening", "Разобрать дебют"),
        BotCommand("help", "Справка"),
    ])


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("opening", opening_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
