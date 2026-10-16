
import os
import re
import json
import hmac
import time
import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", ""))
    if x.strip().lstrip("-").isdigit()
}

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

if not API_KEY:
    raise ValueError("API_KEY not found")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found")

if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS not found")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

MAX_CHANNELS = 10

CONFIG: dict = {
    "enabled": True,
    "channels": [],
}


# ---------------- Database ----------------

def prepare_database_url(url: str) -> str:
    """
    SSL را جداگانه به asyncpg می‌دهیم.
    پارامترهای sslmode و channel_binding از URL حذف می‌شوند
    تا با اتصال مستقیم asyncpg تداخل نداشته باشند.
    """
    parts = urlsplit(url)

    query = [
        (key, value)
        for key, value in parse_qsl(
            parts.query,
            keep_blank_values=True,
        )
        if key.lower() not in ("sslmode", "channel_binding")
    ]

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


class Storage:
    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        dsn = prepare_database_url(DATABASE_URL)

        self.pool = await asyncpg.create_pool(
            dsn=dsn,
            ssl="require",
            min_size=1,
            max_size=5,
            command_timeout=30,
        )

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    channels JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            await conn.execute(
                """
                INSERT INTO admin_config (id, enabled, channels)
                VALUES (1, TRUE, '[]'::jsonb)
                ON CONFLICT (id) DO NOTHING
                """
            )

        logger.info("PostgreSQL connection established")

    async def load(self) -> dict:
        if self.pool is None:
            raise RuntimeError("Database pool is not initialized")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT enabled, channels
                FROM admin_config
                WHERE id = 1
                """
            )

        if row is None:
            return {"enabled": True, "channels": []}

        channels = row["channels"]

        # asyncpg ممکن است JSONB را به شکل رشته برگرداند.
        if isinstance(channels, str):
            channels = json.loads(channels)

        return {
            "enabled": bool(row["enabled"]),
            "channels": list(channels),
        }

    async def save(self, data: dict):
        if self.pool is None:
            raise RuntimeError("Database pool is not initialized")

        channels_json = json.dumps(
            data["channels"],
            ensure_ascii=False,
        )

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admin_config (
                    id,
                    enabled,
                    channels,
                    updated_at
                )
                VALUES (1, $1, $2::jsonb, NOW())
                ON CONFLICT (id)
                DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    channels = EXCLUDED.channels,
                    updated_at = NOW()
                """,
                bool(data["enabled"]),
                channels_json,
            )

    async def close(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None


storage: Storage | None = None


async def persist() -> bool:
    try:
        if storage is None:
            raise RuntimeError("Storage is not initialized")

        await storage.save(CONFIG)
        return True

    except Exception:
        logger.exception("Saving configuration to PostgreSQL failed")
        return False


# ---------------- Membership check ----------------

_member_cache: dict[tuple[int, int], float] = {}
MEMBER_TTL = 30


async def is_member(bot, chat_id: int, user_id: int):
    """
    True: عضو است
    False: عضو نیست
    None: وضعیت نامشخص است
    """
    key = (chat_id, user_id)
    now = time.time()

    cached = _member_cache.get(key)

    if cached and now - cached < MEMBER_TTL:
        return True

    try:
        member = await bot.get_chat_member(chat_id, user_id)

    except TelegramError as e:
        logger.warning(
            "get_chat_member failed for chat %s: %s",
            chat_id,
            e,
        )
        return None

    joined = (
        member.status in ("creator", "administrator", "member")
        or (
            member.status == "restricted"
            and bool(getattr(member, "is_member", False))
        )
    )

    if joined:
        _member_cache[key] = now

    return joined


# ---------------- HTTP API ----------------

def authorized(request: web.Request) -> bool:
    given = request.headers.get("X-API-Key", "")

    return hmac.compare_digest(
        given.encode(),
        API_KEY.encode(),
    )


async def api_root(request: web.Request):
    return web.Response(text="Admin bot is alive")


async def api_check(request: web.Request):
    if not authorized(request):
        return web.json_response(
            {"error": "unauthorized"},
            status=401,
        )

    try:
        user_id = int(request.query.get("user_id", ""))

    except (ValueError, TypeError):
        return web.json_response(
            {"error": "bad user_id"},
            status=400,
        )

    channels = CONFIG["channels"]

    if not CONFIG["enabled"] or not channels:
        return web.json_response(
            {
                "ok": True,
                "enabled": CONFIG["enabled"],
                "missing": [],
            }
        )

    bot = request.app["bot"]

    results = await asyncio.gather(
        *[
            is_member(bot, channel["chat_id"], user_id)
            for channel in channels
        ]
    )

    # فقط مواردی که قطعاً عضویت ندارند گزارش می‌شوند.
    missing = [
        {
            "chat_id": channel["chat_id"],
            "title": channel["title"],
            "link": channel["link"],
        }
        for channel, joined in zip(channels, results)
        if joined is False
    ]

    return web.json_response(
        {
            "ok": not missing,
            "enabled": True,
            "missing": missing,
        }
    )


async def api_channels(request: web.Request):
    if not authorized(request):
        return web.json_response(
            {"error": "unauthorized"},
            status=401,
        )

    return web.json_response(
        {
            "enabled": CONFIG["enabled"],
            "channels": [
                {
                    "chat_id": channel["chat_id"],
                    "title": channel["title"],
                    "link": channel["link"],
                }
                for channel in CONFIG["channels"]
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


# ---------------- Admin panel ----------------

def panel_text() -> str:
    state = "✅ فعال" if CONFIG["enabled"] else "⛔ غیرفعال"

    return (
        "🛠 پنل مدیریت جوین اجباری\n\n"
        f"وضعیت: {state}\n"
        f"تعداد کانال‌ها: {len(CONFIG['channels'])}\n\n"
        "این تنظیمات برای همه‌ی ربات‌های متصل یکسان است."
    )


def panel_markup() -> InlineKeyboardMarkup:
    toggle = (
        "⛔ غیرفعال کردن"
        if CONFIG["enabled"]
        else "✅ فعال کردن"
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ افزودن کانال",
                    callback_data="adm:add",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 کانال‌ها / حذف",
                    callback_data="adm:list",
                )
            ],
            [
                InlineKeyboardButton(
                    toggle,
                    callback_data="adm:toggle",
                )
            ],
        ]
    )


def list_text() -> str:
    if not CONFIG["channels"]:
        return "📋 هنوز کانالی اضافه نشده است."

    lines = ["📋 کانال‌های جوین اجباری:\n"]

    for i, channel in enumerate(CONFIG["channels"], 1):
        lines.append(
            f"{i}. {channel['title']}\n"
            f"   {channel['link']}"
        )

    lines.append("\nبرای حذف، روی دکمه‌ی کانال بزن.")

    return "\n".join(lines)


def list_markup() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"🗑 {channel['title'][:40]}",
                callback_data=f"adm:rm:{channel['chat_id']}",
            )
        ]
        for channel in CONFIG["channels"]
    ]

    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="adm:back",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(
            text,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(
                is_disabled=True
            ),
        )

    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def is_admin(update: Update) -> bool:
    user = update.effective_user

    return bool(user and user.id in ADMIN_IDS)


async def cmd_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ دسترسی نداری."
        )
        return

    context.user_data.pop("awaiting", None)

    await update.effective_message.reply_text(
        panel_text(),
        reply_markup=panel_markup(),
    )


async def cmd_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        return

    context.user_data.pop("awaiting", None)

    await update.effective_message.reply_text(
        "لغو شد.",
        reply_markup=panel_markup(),
    )


async def cmd_chatid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(update):
        await update.effective_message.reply_text(
            "⛔ دسترسی نداری."
        )
        return

    context.user_data["awaiting"] = "chatid"

    await update.effective_message.reply_text(
        "یک پیام فوروارد‌شده از کانال/گروه بفرست "
        "(یا @username یا آیدی عددی) تا آیدی آن را بگویم.\n"
        "لغو: /cancel"
    )


async def on_admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer(
            "⛔ دسترسی نداری.",
            show_alert=True,
        )
        return

    await query.answer()
    data = query.data or ""

    if data == "adm:back":
        context.user_data.pop("awaiting", None)

        await safe_edit(
            query,
            panel_text(),
            panel_markup(),
        )

    elif data == "adm:add":
        context.user_data["awaiting"] = "add"

        await safe_edit(
            query,
            "➕ افزودن کانال\n\n"
            "یکی از این‌ها را بفرست:\n"
            "۱) پیام فوروارد‌شده از کانال\n"
            "۲) @username کانال\n"
            "۳) آیدی عددی مثل -1001234567890\n\n"
            "⚠️ ربات باید در کانال ادمین باشد.\n"
            "لغو: /cancel",
        )

    elif data == "adm:list":
        await safe_edit(
            query,
            list_text(),
            list_markup(),
        )

    elif data == "adm:toggle":
        CONFIG["enabled"] = not CONFIG["enabled"]

        saved = await persist()

        text = panel_text()

        if not saved:
            text += "\n\n⚠️ ذخیره در دیتابیس ناموفق بود."

        await safe_edit(
            query,
            text,
            panel_markup(),
        )

    elif data.startswith("adm:rm:"):
        try:
            chat_id = int(data.split(":", 2)[2])

        except ValueError:
            return

        CONFIG["channels"] = [
            channel
            for channel in CONFIG["channels"]
            if channel["chat_id"] != chat_id
        ]

        saved = await persist()

        text = list_text()

        if not saved:
            text += "\n\n⚠️ ذخیره در دیتابیس ناموفق بود."

        await safe_edit(
            query,
            text,
            list_markup(),
        )


async def resolve_chat_ref(message):
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)

    if origin_chat is not None:
        return origin_chat.id

    text = (message.text or "").strip()

    if re.fullmatch(r"-?\d+", text):
        return int(text)

    if text.startswith("@") and len(text) > 1:
        return text

    match = re.search(
        r"t\.me/([A-Za-z0-9_]{4,})",
        text,
    )

    if match:
        return "@" + match.group(1)

    return None


async def on_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not is_admin(update) or not message:
        return

    mode = context.user_data.get("awaiting")

    if not mode:
        return

    ref = await resolve_chat_ref(message)

    if ref is None:
        await message.reply_text(
            "❌ متوجه نشدم. پیام فوروارد‌شده، "
            "@username یا آیدی عددی بفرست.\n"
            "لغو: /cancel"
        )
        return

    try:
        chat = await context.bot.get_chat(ref)

    except TelegramError:
        await message.reply_text(
            "❌ این چت پیدا نشد. مطمئن شو ربات "
            "عضو/ادمین آن است و آیدی درست است."
        )
        return

    if mode == "chatid":
        context.user_data.pop("awaiting", None)

        await message.reply_text(
            f"🆔 {chat.id}\n{chat.title or ''}"
        )
        return

    if chat.type not in ("channel", "supergroup", "group"):
        await message.reply_text(
            "❌ فقط کانال یا گروه قابل افزودن است."
        )
        return

    if any(
        channel["chat_id"] == chat.id
        for channel in CONFIG["channels"]
    ):
        await message.reply_text(
            "ℹ️ این کانال قبلاً اضافه شده است."
        )
        return

    if len(CONFIG["channels"]) >= MAX_CHANNELS:
        await message.reply_text(
            f"❌ حداکثر {MAX_CHANNELS} کانال مجاز است."
        )
        return

    try:
        me = await context.bot.get_chat_member(
            chat.id,
            context.bot.id,
        )

    except TelegramError:
        me = None

    if not me or me.status not in ("administrator", "creator"):
        await message.reply_text(
            "❌ ربات در این کانال ادمین نیست. "
            "ابتدا آن را ادمین کن و دوباره امتحان کن."
        )
        return

    if chat.username:
        link = f"https://t.me/{chat.username}"

    else:
        link = chat.invite_link

        if not link:
            try:
                link = await context.bot.export_chat_invite_link(
                    chat.id
                )

            except TelegramError:
                link = None

    if not link:
        await message.reply_text(
            "❌ ربات نتوانست لینک دعوت بسازد. "
            "دسترسی ساخت لینک دعوت را بررسی کن."
        )
        return

    CONFIG["channels"].append(
        {
            "chat_id": chat.id,
            "title": chat.title or str(chat.id),
            "link": link,
        }
    )

    saved = await persist()

    context.user_data.pop("awaiting", None)

    suffix = (
        ""
        if saved
        else "\n\n⚠️ ذخیره در دیتابیس انجام نشد؛ "
             "این تغییر ممکن است پس از ری‌استارت از دست برود."
    )

    await message.reply_text(
        f"✅ اضافه شد: {chat.title}\n{link}{suffix}",
        reply_markup=panel_markup(),
    )


async def on_error(
    update,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.warning("Bot error: %s", context.error)


# ---------------- Startup / shutdown ----------------

async def post_init(application: Application):
    global storage, CONFIG

    storage = Storage()

    # اگر دیتابیس در دسترس نباشد، برنامه نباید وانمود کند
    # که تنظیمات مشترک با موفقیت بارگذاری شده‌اند.
    await storage.connect()

    CONFIG = await storage.load()

    logger.info(
        "Config loaded: enabled=%s, channels=%s (storage: PostgreSQL)",
        CONFIG["enabled"],
        len(CONFIG["channels"]),
    )

    runner = web.AppRunner(
        build_web_app(application.bot)
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    application.bot_data["web_runner"] = runner

    logger.info("HTTP API listening on port %s", PORT)


async def post_shutdown(application: Application):
    runner = application.bot_data.get("web_runner")

    if runner:
        await runner.cleanup()

    if storage is not None:
        await storage.close()


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler(
            ["start", "panel"],
            cmd_panel,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cmd_cancel,
        )
    )

    app.add_handler(
        CommandHandler(
            "chatid",
            cmd_chatid,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            on_admin_callback,
            pattern=r"^adm:",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            on_message,
        )
    )

    app.add_error_handler(on_error)

    logger.info("Admin bot is starting...")

    app.run_polling()


if __name__ == "__main__":
    main()
