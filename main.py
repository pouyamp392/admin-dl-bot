import os
import re
import json
import hmac
import time
import asyncio
import logging
from pathlib import Path

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("API_KEY", "")
STORAGE_CHAT_ID_RAW = os.getenv("STORAGE_CHAT_ID", "").strip()
STORAGE_CHAT_ID = int(STORAGE_CHAT_ID_RAW) if STORAGE_CHAT_ID_RAW.lstrip("-").isdigit() else None
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
PORT = int(os.getenv("PORT", "10000"))

ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", ""))
    if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")
if not API_KEY:
    raise ValueError("API_KEY not found (a long random string shared with the other bots)")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS not found (comma-separated numeric Telegram user ids)")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# جلوگیری از چاپ توکن ربات توی لاگ
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

MAX_CHANNELS = 10

# تنظیمات فعلی (در حافظه؛ با هر تغییر ذخیره می‌شود)
CONFIG: dict = {"enabled": True, "channels": []}


# ---------------- Storage ----------------
# اگر STORAGE_CHAT_ID تنظیم شده باشد، تنظیمات داخل «پیام پین‌شده‌ی» یک کانال
# خصوصی ذخیره می‌شود (روی Render رایگان فایل‌ها بعد از ری‌استارت پاک می‌شوند).
# در غیر این صورت داخل فایل data.json ذخیره می‌شود.

class Storage:
    def __init__(self, bot):
        self.bot = bot
        self.message_id = None

    async def load(self) -> dict:
        default = {"enabled": True, "channels": []}

        if STORAGE_CHAT_ID is not None:
            try:
                chat = await self.bot.get_chat(STORAGE_CHAT_ID)
                pinned = chat.pinned_message
                if pinned and pinned.text:
                    data = json.loads(pinned.text)
                    self.message_id = pinned.message_id
                    return {
                        "enabled": bool(data.get("enabled", True)),
                        "channels": list(data.get("channels", [])),
                    }
            except Exception:
                logger.exception("Could not load config from storage chat")
            return default

        if DATA_FILE.exists():
            try:
                data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                return {
                    "enabled": bool(data.get("enabled", True)),
                    "channels": list(data.get("channels", [])),
                }
            except Exception:
                logger.exception("Could not read %s", DATA_FILE)
        return default

    async def save(self, data: dict):
        text = json.dumps(data, ensure_ascii=False)

        if STORAGE_CHAT_ID is not None:
            try:
                if self.message_id:
                    await self.bot.edit_message_text(
                        chat_id=STORAGE_CHAT_ID,
                        message_id=self.message_id,
                        text=text,
                    )
                else:
                    msg = await self.bot.send_message(chat_id=STORAGE_CHAT_ID, text=text)
                    await self.bot.pin_chat_message(
                        chat_id=STORAGE_CHAT_ID,
                        message_id=msg.message_id,
                        disable_notification=True,
                    )
                    self.message_id = msg.message_id
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                raise
            return

        DATA_FILE.write_text(text, encoding="utf-8")


storage: Storage | None = None


async def persist() -> bool:
    try:
        await storage.save(CONFIG)
        return True
    except Exception:
        logger.exception("Saving config failed")
        return False


# ---------------- Membership check ----------------

_member_cache: dict[tuple[int, int], float] = {}
MEMBER_TTL = 30  # فقط نتیجه‌ی «عضو است» را ۳۰ ثانیه کش می‌کنیم


async def is_member(bot, chat_id: int, user_id: int):
    """True: عضو است | False: عضو نیست | None: نامشخص (مثلاً ربات ادمین نیست)."""
    key = (chat_id, user_id)
    now = time.time()
    cached = _member_cache.get(key)
    if cached and now - cached < MEMBER_TTL:
        return True

    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        logger.warning("get_chat_member failed for chat %s: %s", chat_id, e)
        return None

    joined = member.status in ("creator", "administrator", "member") or (
        member.status == "restricted" and bool(getattr(member, "is_member", False))
    )
    if joined:
        _member_cache[key] = now
    return joined


# ---------------- HTTP API (برای بقیه‌ی ربات‌ها) ----------------

def authorized(request: web.Request) -> bool:
    given = request.headers.get("X-API-Key", "")
    return hmac.compare_digest(given.encode(), API_KEY.encode())


async def api_root(request: web.Request):
    return web.Response(text="Admin bot is alive")


async def api_check(request: web.Request):
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        user_id = int(request.query.get("user_id", ""))
    except ValueError:
        return web.json_response({"error": "bad user_id"}, status=400)

    channels = CONFIG["channels"]
    if not CONFIG["enabled"] or not channels:
        return web.json_response(
            {"ok": True, "enabled": CONFIG["enabled"], "missing": []}
        )

    bot = request.app["bot"]
    results = await asyncio.gather(
        *[is_member(bot, c["chat_id"], user_id) for c in channels]
    )

    # فقط کانال‌هایی که «قطعاً عضو نیست» (False)؛ نامشخص‌ها نادیده گرفته می‌شوند
    missing = [
        {"chat_id": c["chat_id"], "title": c["title"], "link": c["link"]}
        for c, joined in zip(channels, results)
        if joined is False
    ]

    return web.json_response(
        {"ok": not missing, "enabled": True, "missing": missing}
    )


async def api_channels(request: web.Request):
    if not authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(
        {
            "enabled": CONFIG["enabled"],
            "channels": [
                {"chat_id": c["chat_id"], "title": c["title"], "link": c["link"]}
                for c in CONFIG["channels"]
            ],
        }
    )


def build_web_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.add_routes(
        [
            web.get("/", api_root),
            web.get("/api/check", api_check),
            web.get("/api/channels", api_channels),
        ]
    )
    return app


# ---------------- Panel UI ----------------

def panel_text() -> str:
    state = "✅ فعال" if CONFIG["enabled"] else "⛔ غیرفعال"
    return (
        "🛠 پنل مدیریت جوین اجباری\n\n"
        f"وضعیت: {state}\n"
        f"تعداد کانال‌ها: {len(CONFIG['channels'])}\n\n"
        "این تنظیمات برای همه‌ی ربات‌ها یکسان است."
    )


def panel_markup() -> InlineKeyboardMarkup:
    toggle = "⛔ غیرفعال کردن" if CONFIG["enabled"] else "✅ فعال کردن"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزودن کانال", callback_data="adm:add")],
            [InlineKeyboardButton("📋 کانال‌ها / حذف", callback_data="adm:list")],
            [InlineKeyboardButton(toggle, callback_data="adm:toggle")],
        ]
    )


def list_text() -> str:
    if not CONFIG["channels"]:
        return "📋 هنوز کانالی اضافه نشده است."
    lines = ["📋 کانال‌های جوین اجباری:\n"]
    for i, c in enumerate(CONFIG["channels"], 1):
        lines.append(f"{i}. {c['title']}\n   {c['link']}")
    lines.append("\nبرای حذف، روی دکمه‌ی کانال بزن.")
    return "\n".join(lines)


def list_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🗑 {c['title'][:40]}", callback_data=f"adm:rm:{c['chat_id']}")]
        for c in CONFIG["channels"]
    ]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm:back")])
    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ دسترسی نداری.")
        return
    context.user_data.pop("awaiting", None)
    await update.effective_message.reply_text(panel_text(), reply_markup=panel_markup())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop("awaiting", None)
    await update.effective_message.reply_text("لغو شد.", reply_markup=panel_markup())


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ دسترسی نداری.")
        return
    context.user_data["awaiting"] = "chatid"
    await update.effective_message.reply_text(
        "یک پیام فوروارد‌شده از کانال/گروه بفرست (یا @username یا آیدی عددی) "
        "تا آیدی عددیش را بگویم.\nلغو: /cancel"
    )


async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ دسترسی نداری.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""

    if data == "adm:back":
        context.user_data.pop("awaiting", None)
        await safe_edit(query, panel_text(), panel_markup())

    elif data == "adm:add":
        context.user_data["awaiting"] = "add"
        await safe_edit(
            query,
            "➕ افزودن کانال\n\n"
            "یکی از این‌ها را بفرست:\n"
            "۱) یک پیام فوروارد‌شده از آن کانال\n"
            "۲) @username کانال\n"
            "۳) آیدی عددی (مثل -1001234567890)\n\n"
            "⚠️ ربات باید در آن کانال ادمین باشد.\n"
            "لغو: /cancel",
        )

    elif data == "adm:list":
        await safe_edit(query, list_text(), list_markup())

    elif data == "adm:toggle":
        CONFIG["enabled"] = not CONFIG["enabled"]
        saved = await persist()
        text = panel_text() + ("" if saved else "\n\n⚠️ ذخیره‌ی تنظیمات انجام نشد.")
        await safe_edit(query, text, panel_markup())

    elif data.startswith("adm:rm:"):
        try:
            chat_id = int(data.split(":", 2)[2])
        except ValueError:
            return
        CONFIG["channels"] = [c for c in CONFIG["channels"] if c["chat_id"] != chat_id]
        saved = await persist()
        text = list_text() + ("" if saved else "\n\n⚠️ ذخیره‌ی تنظیمات انجام نشد.")
        await safe_edit(query, text, list_markup())


async def resolve_chat_ref(message):
    """از پیام ادمین، یک مرجع چت (آیدی عددی یا @username) درمی‌آورد."""
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)
    if origin_chat is not None:
        return origin_chat.id

    text = (message.text or "").strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text.startswith("@") and len(text) > 1:
        return text
    match = re.search(r"t\.me/([A-Za-z0-9_]{4,})", text)
    if match:
        return "@" + match.group(1)
    return None


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not is_admin(update) or not message:
        return

    mode = context.user_data.get("awaiting")
    if not mode:
        return

    ref = await resolve_chat_ref(message)
    if ref is None:
        await message.reply_text(
            "❌ نفهمیدم. پیام فوروارد‌شده از کانال، @username یا آیدی عددی بفرست. (لغو: /cancel)"
        )
        return

    try:
        chat = await context.bot.get_chat(ref)
    except TelegramError:
        await message.reply_text(
            "❌ این چت پیدا نشد. مطمئن شو ربات عضو/ادمین آن است و آیدی درست است."
        )
        return

    if mode == "chatid":
        context.user_data.pop("awaiting", None)
        await message.reply_text(f"🆔 {chat.id}\n{chat.title or ''}")
        return

    # ---- افزودن کانال ----
    if chat.type not in ("channel", "supergroup", "group"):
        await message.reply_text("❌ فقط کانال یا گروه قابل افزودن است.")
        return

    if any(c["chat_id"] == chat.id for c in CONFIG["channels"]):
        await message.reply_text("ℹ️ این کانال قبلاً اضافه شده است.")
        return

    if len(CONFIG["channels"]) >= MAX_CHANNELS:
        await message.reply_text(f"❌ حداکثر {MAX_CHANNELS} کانال مجاز است.")
        return

    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
    except TelegramError:
        me = None
    if not me or me.status not in ("administrator", "creator"):
        await message.reply_text(
            "❌ ربات در این کانال ادمین نیست. اول ربات ادمین را در کانال ادمین کن "
            "(برای چک کردن عضویت لازم است) و دوباره امتحان کن."
        )
        return

    if chat.username:
        link = f"https://t.me/{chat.username}"
    else:
        link = chat.invite_link
        if not link:
            try:
                link = await context.bot.export_chat_invite_link(chat.id)
            except TelegramError:
                link = None
    if not link:
        await message.reply_text(
            "❌ کانال خصوصی است و ربات نتوانست لینک دعوت بسازد. "
            "به ربات دسترسی «Invite users via link» بده و دوباره امتحان کن."
        )
        return

    CONFIG["channels"].append(
        {"chat_id": chat.id, "title": chat.title or str(chat.id), "link": link}
    )
    saved = await persist()
    context.user_data.pop("awaiting", None)

    suffix = "" if saved else "\n\n⚠️ ذخیره‌ی تنظیمات انجام نشد (بعد از ری‌استارت پاک می‌شود)."
    await message.reply_text(
        f"✅ اضافه شد: {chat.title}\n{link}{suffix}",
        reply_markup=panel_markup(),
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    # خطاهای شبکه‌ی گذرا (مثل Conflict هنگام دیپلوی) را فقط کوتاه لاگ می‌کنیم
    logger.warning("Bot error: %s", context.error)


# ---------------- Startup / shutdown ----------------

async def post_init(application: Application):
    global storage, CONFIG

    storage = Storage(application.bot)
    CONFIG = await storage.load()
    logger.info(
        "Config loaded: enabled=%s, channels=%s (storage: %s)",
        CONFIG["enabled"],
        len(CONFIG["channels"]),
        "telegram-pinned-message" if STORAGE_CHAT_ID is not None else "local-file",
    )

    runner = web.AppRunner(build_web_app(application.bot))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    application.bot_data["web_runner"] = runner
    logger.info("HTTP API listening on port %s", PORT)


async def post_shutdown(application: Application):
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler(["start", "panel"], cmd_panel))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^adm:"))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message)
    )
    app.add_error_handler(on_error)

    logger.info("Admin bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
