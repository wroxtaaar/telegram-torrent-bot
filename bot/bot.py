import os
import json
import asyncio
import secrets
import shutil
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

from dotenv import load_dotenv
from qbittorrentapi import Client
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

QBIT_HOST = os.environ.get("QBIT_HOST", "qbittorrent")
QBIT_PORT = int(os.environ.get("QBIT_PORT", "8080"))
QBIT_USERNAME = os.environ["QBITTORRENT_USERNAME"]
QBIT_PASSWORD = os.environ["QBITTORRENT_PASSWORD"]

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")

DOWNLOAD_DIR = Path(
    os.environ.get("DOWNLOAD_DIR", "/downloads")
).resolve()

# Temporary in-memory link tokens.
# They disappear if the bot restarts.
TOKEN_FILE = DOWNLOAD_DIR / ".file_tokens.json"
COMPLETED_FILE = DOWNLOAD_DIR / ".completed_torrents.json"


def load_completed_torrents():
    try:
        if COMPLETED_FILE.exists():
            with COMPLETED_FILE.open("r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
    except Exception as e:
        print(f"Completion state load error: {e}")

    return set()


def save_completed_torrents(completed):
    try:
        with COMPLETED_FILE.open("w") as f:
            json.dump(sorted(completed), f)
    except Exception as e:
        print(f"Completion state save error: {e}")


COMPLETED_TORRENTS = load_completed_torrents()

# Temporary file-selection state:
# {
#   torrent_hash: {
#       "files": [...],
#       "selected": {file_index, ...}
#   }
# }
FILE_SELECTIONS = {}
FILE_PROGRESS_TASKS = {}
LIST_REFRESH_TASKS = {}
SCREEN_STATES = {}
TELEGRAM_APPLICATION = None



def extract_magnet_hash(magnet):
    """Extract and normalize the BTIH from a magnet link."""
    try:
        parsed = urlparse(magnet)
        params = parse_qs(parsed.query)

        xt_values = params.get("xt", [])

        for xt in xt_values:
            prefix = "urn:btih:"

            if not xt.lower().startswith(prefix):
                continue

            btih = xt[len(prefix):].strip()

            # Standard magnet hashes are usually 40-character hex.
            if len(btih) == 40:
                return btih.lower()

            # Some magnets use a 32-character Base32 BTIH.
            if len(btih) == 32:
                import base64

                padded = btih.upper() + "=" * (
                    (-len(btih)) % 8
                )

                decoded = base64.b32decode(padded)

                return decoded.hex()

    except Exception as e:
        print(f"Magnet hash extraction error: {e}")

    return None



def save_token(token, file_path):
    try:
        if TOKEN_FILE.exists():
            with TOKEN_FILE.open("r") as f:
                tokens = json.load(f)
        else:
            tokens = {}
    except Exception:
        tokens = {}

    tokens[token] = str(file_path)

    with TOKEN_FILE.open("w") as f:
        json.dump(tokens, f)


def is_authorized(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == ALLOWED_USER_ID
    )


def get_qbittorrent():
    client = Client(
        host=QBIT_HOST,
        port=QBIT_PORT,
        username=QBIT_USERNAME,
        password=QBIT_PASSWORD,
    )

    client.auth_log_in()
    return client


def format_size(size):
    size = float(size)

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

    return f"{size:.2f} PB"


def format_speed(speed):
    return format_size(speed) + "/s"


def torrent_buttons(torrent_hash):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔗 Link",
                    callback_data=f"link:{torrent_hash}",
                ),
                InlineKeyboardButton(
                    "⏹ Stop",
                    callback_data=f"stop:{torrent_hash}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "▶️ Start",
                    callback_data=f"start:{torrent_hash}",
                ),
                InlineKeyboardButton(
                    "🗑 Delete",
                    callback_data=f"delete:{torrent_hash}",
                ),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    await update.message.reply_text(
        "🚀 Telegram Torrent Bot\n\n"
        "Send a magnet link directly or use:\n"
        "/magnet <link>\n\n"
        "Commands:\n"
        "/list - List torrents\n"
        "/link <hash> - Get file link\n"
        "/stop <hash> - Stop torrent\n"
        "/starttorrent <hash> - Start torrent\n"
        "/delete <hash> - Delete torrent + files"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    await update.message.reply_text(
        "Commands:\n\n"
        "/start - Start bot\n"
        "/test - Test qBittorrent\n"
        "/magnet <link> - Add torrent\n"
        "/list - List torrents\n"
        "/link <hash> - Get completed file link\n"
        "/stop <hash> - Stop torrent\n"
        "/starttorrent <hash> - Resume torrent\n"
        "/delete <hash> - Delete torrent + downloaded files"
    )


async def test_qbittorrent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    try:
        qb = get_qbittorrent()
        version = qb.app.version

        await update.message.reply_text(
            f"✅ qBittorrent connection successful!\n\n"
            f"Version: {version}"
        )

    except Exception as e:
        print(f"qBittorrent error: {e}")
        await update.message.reply_text(
            "❌ Could not connect to qBittorrent."
        )


async def add_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n\n"
            "/magnet magnet:?xt=urn:btih:..."
        )
        return

    magnet = " ".join(context.args).strip()

    if not magnet.startswith("magnet:?"):
        await update.message.reply_text(
            "❌ That doesn't look like a magnet link."
        )
        return

    await add_magnet_link(update, magnet)


async def add_magnet_link(update: Update, magnet: str):
    try:
        qb = get_qbittorrent()

        # Extract the BTIH before adding the magnet.
        torrent_hash = extract_magnet_hash(magnet)

        if not torrent_hash:
            await update.message.reply_text(
                "❌ Could not extract the torrent hash from this magnet."
            )
            return

        print(f"🧲 Magnet BTIH: {torrent_hash}")

        # Let qBittorrent retrieve the magnet metadata, then
        # automatically stop the torrent immediately after the
        # metadata is received.
        result = qb.torrents_add(
            urls=magnet,
            save_path=str(DOWNLOAD_DIR),
            stop_condition="MetadataReceived",
        )

        print(f"Magnet add result: {result}")

        asyncio.create_task(
            wait_for_metadata_and_show_files(
                update.effective_chat.id,
                torrent_hash,
            )
        )

    except Exception as e:
        print(f"Magnet error: {e}")

        if str(e).strip().lower() == "conflict":
            await update.message.reply_text(
                "⚠️ Torrent already exists.\n\n"
                "📥 It is already in qBittorrent."
            )
        else:
            await update.message.reply_text(
                "❌ Failed to add torrent.\n\n"
                f"Error: {e}"
            )




async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = (update.message.text or "").strip()

    if not text.startswith("magnet:?"):
        return

    await add_magnet_link(update, text)



def file_selection_keyboard(torrent_hash, files, selected):
    keyboard = []

    for file in files:
        index = int(getattr(file, "index", 0))
        name = str(getattr(file, "name", f"File {index}"))
        display_name = Path(name).name
        size = getattr(file, "size", 0)

        checked = "☑️" if index in selected else "☐"

        # Keep callback data short enough for Telegram.
        keyboard.append([
            InlineKeyboardButton(
                f"{checked} {display_name[:55]} ({format_size(size)})",
                callback_data=f"filetoggle:{torrent_hash}:{index}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "☑️ Select All",
            callback_data=f"fileall:{torrent_hash}",
        ),
        InlineKeyboardButton(
            "❌ Clear All",
            callback_data=f"fileclear:{torrent_hash}",
        ),
    ])

    keyboard.append([
        InlineKeyboardButton(
            "▶️ Download Selected",
            callback_data=f"filestart:{torrent_hash}",
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "❌ Cancel",
            callback_data=f"filecancel:{torrent_hash}",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


def build_file_selection_text(torrent, files, selected):
    selected_count = len(selected)
    total_count = len(files)

    selected_size = sum(
        getattr(file, "size", 0)
        for file in files
        if int(getattr(file, "index", 0)) in selected
    )

    return (
        f"📁 <b>Select files to download</b>\n\n"
        f"📦 <b>{torrent.name}</b>\n\n"
        f"☑️ Selected: <b>{selected_count}/{total_count}</b>\n"
        f"💾 Selected size: <b>{format_size(selected_size)}</b>\n\n"
        f"Tap a file to select or deselect it."
    )


async def show_file_selector(message, qb, torrent_hash):
    torrent = get_torrent(qb, torrent_hash)

    if not torrent:
        await message.reply_text("❌ Torrent not found.")
        return

    # Metadata is not available yet.
    if torrent.state in ("metaDL", "checkingResumeData"):
        return False

    try:
        files = qb.torrents_files(torrent_hash=torrent_hash)
    except Exception as e:
        print(f"File list error: {e}")
        return False

    if not files:
        return False

    # Start with all files selected.
    selected = {
        int(getattr(file, "index", 0))
        for file in files
    }

    FILE_SELECTIONS[torrent_hash] = {
        "files": files,
        "selected": selected,
    }

    await message.reply_text(
        build_file_selection_text(
            torrent,
            files,
            selected,
        ),
        parse_mode="HTML",
        reply_markup=file_selection_keyboard(
            torrent_hash,
            files,
            selected,
        ),
    )

    return True


async def wait_for_metadata_and_show_files(
    chat_id,
    torrent_hash,
):
    """
    Wait for qBittorrent to obtain magnet metadata, then show
    the file-selection UI.
    """
    try:
        for attempt in range(60):
            await asyncio.sleep(2)

            try:
                qb = get_qbittorrent()
                torrent = get_torrent(qb, torrent_hash)

                if not torrent:
                    print(
                        f"⏳ Torrent not visible yet: "
                        f"{torrent_hash} "
                        f"(attempt {attempt + 1}/60)"
                    )
                    continue

                print(
                    f"📡 Metadata state: "
                    f"{torrent.name} -> {torrent.state}"
                )

                # Try to retrieve files.
                try:
                    files = qb.torrents_files(
                        torrent_hash=torrent_hash
                    )
                except Exception as file_error:
                    print(
                        f"⏳ Files not ready yet: {file_error}"
                    )
                    continue

                if not files:
                    print(
                        f"⏳ No files yet "
                        f"(attempt {attempt + 1}/60)"
                    )
                    continue

                if len(files) == 1:
                    file_index = int(getattr(files[0], "index", 0))
                    qb.torrents_file_priority(
                        torrent_hash=torrent_hash,
                        file_ids=[file_index],
                        priority=1,
                    )
                    qb.torrents_start(torrent_hashes=torrent_hash)

                    await TELEGRAM_APPLICATION.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🚀 <b>Magnet detected!</b>\n\n"
                            "▶️ <b>Starting download...</b>"
                        ),
                        parse_mode="HTML",
                    )

                    status_text, torrents = await build_torrent_status(qb)
                    list_message = await TELEGRAM_APPLICATION.bot.send_message(
                        chat_id=chat_id,
                        text=status_text,
                        reply_markup=list_keyboard(torrents),
                        parse_mode="HTML",
                    )
                    start_torrent_list_refresh(
                        TELEGRAM_APPLICATION,
                        chat_id,
                        list_message.message_id,
                    )

                    print(
                        f"▶️ Single-file download started: "
                        f"{torrent.name}"
                    )
                    return

                selected = {
                    int(getattr(file, "index", 0))
                    for file in files
                }

                FILE_SELECTIONS[torrent_hash] = {
                    "files": files,
                    "selected": selected,
                }

                await application_bot_send_file_selector(
                    chat_id,
                    torrent,
                    files,
                    selected,
                    torrent_hash,
                )

                print(
                    f"📁 File selector sent: "
                    f"{torrent.name} "
                    f"({len(files)} files)"
                )

                return

            except Exception as e:
                print(
                    f"Metadata wait error "
                    f"(attempt {attempt + 1}/60): {e}"
                )

        print(
            f"⏰ Metadata timeout: {torrent_hash}"
        )

        if TELEGRAM_APPLICATION is not None:
            await TELEGRAM_APPLICATION.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏰ <b>Metadata timeout</b>\n\n"
                    "qBittorrent did not receive the torrent "
                    "file list within 2 minutes.\n\n"
                    "The torrent remains paused."
                ),
                parse_mode="HTML",
            )

    except asyncio.CancelledError:
        return

    except Exception as e:
        print(f"Metadata watcher error: {e}")




async def application_bot_send_file_selector(
    chat_id,
    torrent,
    files,
    selected,
    torrent_hash,
):
    # This uses the Telegram application stored globally by main().
    if TELEGRAM_APPLICATION is None:
        print("❌ Telegram application is not available.")
        return

    await TELEGRAM_APPLICATION.bot.send_message(
        chat_id=chat_id,
        text=build_file_selection_text(
            torrent,
            files,
            selected,
        ),
        parse_mode="HTML",
        reply_markup=file_selection_keyboard(
            torrent_hash,
            files,
            selected,
        ),
    )



def format_file_size(size):
    size = float(size or 0)

    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"

    return f"{int(size)} B"


def progress_bar(progress, width=12):
    progress = max(0.0, min(1.0, float(progress or 0)))
    filled = int(progress * width)

    if filled >= width:
        return "█" * width

    return "█" * filled + "░" * (width - filled)





async def refresh_file_progress_message(
    application,
    chat_id,
    message_id,
    torrent_hash,
):
    """
    Auto-refresh File Progress.

    IMPORTANT:
    This task is allowed to edit the Telegram message only while
    SCREEN_STATES[key] == "file_progress".

    If the user returns to the torrent list, the task immediately
    stops being allowed to edit that message.
    """
    key = f"{chat_id}:{message_id}"

    print(
        f"🔄 File Progress refresh started: "
        f"message={message_id}, torrent={torrent_hash}"
    )

    try:
        while True:
            await asyncio.sleep(1)

            # ------------------------------------------------
            # HARD SCREEN GUARD
            # ------------------------------------------------
            if SCREEN_STATES.get(key) != "file_progress":
                print(
                    f"🛑 File Progress refresh blocked: "
                    f"message={message_id}, "
                    f"screen={SCREEN_STATES.get(key)}"
                )
                return

            current_task = FILE_PROGRESS_TASKS.get(key)

            if current_task is not asyncio.current_task():
                print(
                    f"🛑 Old File Progress task stopped: "
                    f"message={message_id}"
                )
                return

            try:
                qb = get_qbittorrent()
                torrent = get_torrent(qb, torrent_hash)

                if not torrent:
                    return

                text = await build_file_progress(
                    qb,
                    torrent_hash,
                )

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔄 Refresh",
                            callback_data=f"files:{torrent_hash}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Back to List",
                            callback_data="menu_list",
                        )
                    ],
                ])

                # Check AGAIN immediately before editing.
                if SCREEN_STATES.get(key) != "file_progress":
                    return

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

            except Exception as e:
                print(
                    f"⚠️ File Progress refresh error: {e}"
                )

    except asyncio.CancelledError:
        print(
            f"🛑 File Progress task cancelled: "
            f"message={message_id}"
        )
        raise

    finally:
        if FILE_PROGRESS_TASKS.get(key) is asyncio.current_task():
            FILE_PROGRESS_TASKS.pop(key, None)

async def build_file_progress(qb, torrent_hash):
    torrent = get_torrent(qb, torrent_hash)

    if not torrent:
        return "❌ Torrent not found."

    files = qb.torrents_files(torrent_hash=torrent_hash)

    if not files:
        return (
            f"📁 <b>{torrent.name}</b>\n\n"
            "⏳ File information is not available yet."
        )

    lines = [
        f"📊 <b>File Progress</b>",
        f"📦 <b>{torrent.name}</b>",
        "",
    ]

    selected_count = 0
    completed_count = 0

    for file in files:
        name = str(getattr(file, "name", "Unknown file"))
        progress = float(getattr(file, "progress", 0) or 0)
        size = int(getattr(file, "size", 0) or 0)
        priority = int(getattr(file, "priority", 0) or 0)

        percent = progress * 100

        if progress >= 0.999999:
            icon = "✅"
            completed_count += 1
        elif priority == 0:
            icon = "⏭️"
        elif progress > 0:
            icon = "⬇️"
            selected_count += 1
        else:
            icon = "⏳"
            selected_count += 1

        # Keep Telegram messages readable for deeply nested torrent paths.
        display_name = name
        if len(display_name) > 70:
            display_name = "..." + display_name[-67:]

        bar = progress_bar(progress)

        lines.append(
            f"{icon} <code>{display_name}</code>\n"
            f"   {bar} <b>{percent:.1f}%</b>"
            f"  ({format_file_size(size)})"
        )

    lines.extend([
        "",
        f"📁 Files: <b>{len(files)}</b>",
        f"✅ Completed: <b>{completed_count}</b>",
    ])

    return "\n".join(lines)


async def build_torrent_status(qb):
    torrents = qb.torrents_info()

    if not torrents:
        return "📭 No torrents found.", None

    messages = []

    for torrent in torrents:
        progress = torrent.progress * 100
        downloaded = getattr(torrent, "downloaded", 0)
        total_size = getattr(torrent, "size", 0)
        speed = getattr(torrent, "dlspeed", 0)

        if torrent.progress >= 1:
            status = "✅ Completed"
        elif torrent.state in ("downloading", "forcedDL"):
            status = "📥 Downloading"
        elif torrent.state in ("stoppedDL", "stoppedUP"):
            status = "⏹ Stopped"
        elif torrent.state == "metaDL":
            status = "🔎 Getting metadata"
        else:
            status = f"📡 {torrent.state}"

        messages.append(
            f"📦 <b>{torrent.name}</b>\n\n"
            f"📊 Progress: <b>{progress:.1f}%</b>\n"
            f"💾 {format_size(downloaded)} / {format_size(total_size)}\n"
            f"⬇️ Speed: {format_speed(speed)}\n"
            f"📡 Status: {status}\n\n"
            f"🔑 <code>{torrent.hash}</code>"
        )

    return "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(messages), torrents


def list_keyboard(torrents):
    keyboard = []

    # Individual file-progress button
    if torrents:
        keyboard.append([
            InlineKeyboardButton(
                "📊 File Progress",
                callback_data=f"files:{torrents[0].hash}",
            )
        ])

    for torrent in torrents:
        # Refresh button
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"🔄 {torrent.name[:25]}",
                    callback_data=f"refresh:{torrent.hash}",
                )
            ]
        )

        # Completed torrent
        if torrent.progress >= 1:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🔗 Link",
                        callback_data=f"link:{torrent.hash}",
                    ),
                    InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data=f"delete:{torrent.hash}",
                    ),
                ]
            )

        # Currently downloading
        elif torrent.state in (
            "downloading",
            "forcedDL",
            "queuedDL",
            "stalledDL",
            "checkingDL",
        ):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "⏹ Stop",
                        callback_data=f"stop:{torrent.hash}",
                    ),
                    InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data=f"delete:{torrent.hash}",
                    ),
                ]
            )

        # Stopped / paused
        elif torrent.state in (
            "stoppedDL",
            "stoppedUP",
            "pausedDL",
            "pausedUP",
        ):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "▶️ Start",
                        callback_data=f"start:{torrent.hash}",
                    ),
                    InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data=f"delete:{torrent.hash}",
                    ),
                ]
            )

        # Other states
        else:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data=f"delete:{torrent.hash}",
                    )
                ]
            )

    return InlineKeyboardMarkup(keyboard)


async def refresh_torrent_list_message(
    application,
    chat_id,
    message_id,
):
    key = f"{chat_id}:{message_id}"

    print(
        f"🔄 Torrent list refresh started: message={message_id}"
    )

    try:
        while True:
            await asyncio.sleep(1)

            if SCREEN_STATES.get(key) != "list":
                return

            if LIST_REFRESH_TASKS.get(key) is not asyncio.current_task():
                return

            try:
                qb = get_qbittorrent()
                status_text, torrents = await build_torrent_status(qb)

                if SCREEN_STATES.get(key) != "list":
                    return

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=status_text,
                    parse_mode="HTML",
                    reply_markup=list_keyboard(torrents),
                )

            except Exception as error:
                if "Message is not modified" not in str(error):
                    print(f"⚠️ Torrent list refresh error: {error}")

    except asyncio.CancelledError:
        raise

    finally:
        if LIST_REFRESH_TASKS.get(key) is asyncio.current_task():
            LIST_REFRESH_TASKS.pop(key, None)


def start_torrent_list_refresh(application, chat_id, message_id):
    key = f"{chat_id}:{message_id}"
    old_task = LIST_REFRESH_TASKS.pop(key, None)

    if old_task is not None:
        old_task.cancel()

    SCREEN_STATES[key] = "list"
    LIST_REFRESH_TASKS[key] = asyncio.create_task(
        refresh_torrent_list_message(
            application,
            chat_id,
            message_id,
        )
    )




async def list_torrents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qb = get_qbittorrent()

        # build_torrent_status expects the qBittorrent client.
        status_text, torrents = await build_torrent_status(qb)

        message = await update.message.reply_text(
            status_text,
            reply_markup=list_keyboard(torrents),
            parse_mode="HTML",
        )

        start_torrent_list_refresh(
            context.application,
            message.chat_id,
            message.message_id,
        )

    except Exception as e:
        print(f"List error: {e}")
        await update.message.reply_text(
            "❌ Failed to retrieve torrents."
        )



def get_torrent(qb, torrent_hash):
    torrents = qb.torrents_info(torrent_hashes=torrent_hash)

    if not torrents:
        return None

    return torrents[0]


async def stop_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/stop <torrent-hash>"
        )
        return

    torrent_hash = context.args[0]

    try:
        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await message.reply_text("❌ Torrent not found.")
            return

        qb.torrents_stop(torrent_hashes=torrent_hash)

        await update.message.reply_text(
            f"⏹ Stopped:\n{torrent.name}"
        )

    except Exception as e:
        print(f"Stop error: {e}")
        await update.message.reply_text(
            f"❌ Failed to stop torrent:\n{e}"
        )


async def start_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/starttorrent <torrent-hash>"
        )
        return

    torrent_hash = context.args[0]

    try:
        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await message.reply_text("❌ Torrent not found.")
            return

        qb.torrents_start(torrent_hashes=torrent_hash)

        await update.message.reply_text(
            f"▶️ Started:\n{torrent.name}"
        )

    except Exception as e:
        print(f"Start error: {e}")
        await update.message.reply_text(
            f"❌ Failed to start torrent:\n{e}"
        )


async def delete_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/delete <torrent-hash>"
        )
        return

    torrent_hash = context.args[0]

    try:
        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await message.reply_text("❌ Torrent not found.")
            return

        name = torrent.name

        qb.torrents_delete(
            torrent_hashes=torrent_hash,
            delete_files=True,
        )

        await update.message.reply_text(
            f"🗑 Deleted torrent and downloaded files:\n\n{name}"
        )

    except Exception as e:
        print(f"Delete error: {e}")
        await update.message.reply_text(
            f"❌ Failed to delete torrent:\n{e}"
        )


async def create_links(message, torrent_hash: str):
    try:
        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await message.reply_text("❌ Torrent not found.")
            return

        if torrent.progress < 1:
            await message.reply_text(
                "⏳ Torrent is not complete yet."
            )
            return

        files = qb.torrents_files(torrent_hash=torrent_hash)

        if not files:
            await message.reply_text(
                "❌ No files found."
            )
            return

        links = []

        for file in files:
            relative_path = Path(file.name)

            if not relative_path.is_absolute():
                file_path = DOWNLOAD_DIR / relative_path
            else:
                file_path = relative_path

            file_path = file_path.resolve()

            try:
                file_path.relative_to(DOWNLOAD_DIR.resolve())
            except ValueError:
                continue

            if not file_path.is_file():
                continue

            token = secrets.token_urlsafe(24)

            save_token(token, file_path)

            url = (
                f"{BASE_URL}/files/{token}/"
                f"{quote(file_path.name)}"
            )

            links.append(
                f"🎬 {file_path.name}\n"
                f"{url}"
            )

        if not links:
            await message.reply_text(
                "❌ Completed files could not be found."
            )
            return

        await message.reply_text(
            "✅ Torrent complete!\n\n"
            + "\n\n".join(links)
        )

    except Exception as e:
        print(f"Link error: {e}")
        await message.reply_text(
            f"❌ Failed to create link:\n{e}"
        )


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/link <torrent-hash>"
        )
        return

    await create_links(update, context.args[0])


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()

    # -----------------------------------------------------
    # File-selection buttons
    # -----------------------------------------------------

    if query.data.startswith("filetoggle:"):
        _, torrent_hash, index_text = query.data.split(":", 2)
        index = int(index_text)

        selection = FILE_SELECTIONS.get(torrent_hash)

        if not selection:
            await query.answer(
                "File selection expired. Open the torrent again.",
                show_alert=True,
            )
            return

        selected = selection["selected"]

        if index in selected:
            selected.remove(index)
        else:
            selected.add(index)

        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await query.answer("Torrent not found.", show_alert=True)
            return

        await query.edit_message_text(
            build_file_selection_text(
                torrent,
                selection["files"],
                selected,
            ),
            parse_mode="HTML",
            reply_markup=file_selection_keyboard(
                torrent_hash,
                selection["files"],
                selected,
            ),
        )

        return

    if query.data.startswith("fileall:"):
        _, torrent_hash = query.data.split(":", 1)

        selection = FILE_SELECTIONS.get(torrent_hash)

        if not selection:
            await query.answer(
                "File selection expired.",
                show_alert=True,
            )
            return

        selection["selected"] = {
            int(getattr(file, "index", 0))
            for file in selection["files"]
        }

        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await query.answer("Torrent not found.", show_alert=True)
            return

        await query.edit_message_text(
            build_file_selection_text(
                torrent,
                selection["files"],
                selection["selected"],
            ),
            parse_mode="HTML",
            reply_markup=file_selection_keyboard(
                torrent_hash,
                selection["files"],
                selection["selected"],
            ),
        )

        return

    if query.data.startswith("fileclear:"):
        _, torrent_hash = query.data.split(":", 1)

        selection = FILE_SELECTIONS.get(torrent_hash)

        if not selection:
            await query.answer(
                "File selection expired.",
                show_alert=True,
            )
            return

        selection["selected"] = set()

        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await query.answer("Torrent not found.", show_alert=True)
            return

        await query.edit_message_text(
            build_file_selection_text(
                torrent,
                selection["files"],
                selection["selected"],
            ),
            parse_mode="HTML",
            reply_markup=file_selection_keyboard(
                torrent_hash,
                selection["files"],
                selection["selected"],
            ),
        )

        return

    if query.data.startswith("filestart:"):
        # -----------------------------------------------
        # Leave File Progress screen permanently.
        # -----------------------------------------------
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        progress_key = f"{chat_id}:{message_id}"

        # Change screen FIRST.
        # Any old File Progress task is now forbidden from
        # editing this Telegram message.
        SCREEN_STATES[progress_key] = "list"

        old_progress_task = FILE_PROGRESS_TASKS.pop(
            progress_key,
            None,
        )

        if old_progress_task is not None:
            old_progress_task.cancel()

        print(
            f"📋 Returning message {message_id} to torrent list"
        )

        _, torrent_hash = query.data.split(":", 1)

        # Stop the File Progress auto-refresh BEFORE returning to /list.
        # Otherwise the old background task will edit this same Telegram
        # message back to File Progress every few seconds.
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        progress_key = f"{chat_id}:{message_id}"

        old_progress_task = FILE_PROGRESS_TASKS.pop(progress_key, None)

        if old_progress_task is not None:
            old_progress_task.cancel()
            print(
                f"🛑 File Progress refresh stopped for "
                f"message {message_id}"
            )

        print("🟢 FILESTART CALLBACK RECEIVED")

        _, torrent_hash = query.data.split(":", 1)
        print(f"🟢 Torrent hash: {torrent_hash}")

        selection = FILE_SELECTIONS.get(torrent_hash)

        if not selection:
            print("❌ File selection not found")
            await query.answer(
                "Selection expired. Please add the magnet again.",
                show_alert=True,
            )
            return

        selected = selection["selected"]
        files = selection["files"]

        print(f"🟢 Total files: {len(files)}")
        print(f"🟢 Selected indexes: {sorted(selected)}")

        try:
            qb = get_qbittorrent()

            all_indexes = [
                int(getattr(file, "index", 0))
                for file in files
            ]

            print(f"🟢 All file indexes: {all_indexes}")

            # Disable every file first.
            if all_indexes:
                print("🟡 Setting all files to priority 0...")
                qb.torrents_file_priority(
                    torrent_hash=torrent_hash,
                    file_ids=all_indexes,
                    priority=0,
                )
                print("✅ All files set to priority 0")

            # Enable only selected files.
            selected_indexes = [
                int(index)
                for index in selected
            ]

            print(f"🟡 Starting selected files: {selected_indexes}")

            if selected_indexes:
                qb.torrents_file_priority(
                    torrent_hash=torrent_hash,
                    file_ids=selected_indexes,
                    priority=1,
                )
                print("✅ Selected files set to priority 1")

            print("🟡 Starting torrent...")
            qb.torrents_start(
                torrent_hashes=torrent_hash
            )
            print("✅ Torrent start command sent")

            torrent = get_torrent(qb, torrent_hash)

            if torrent:
                print(
                    f"🟢 Torrent state after start: "
                    f"{torrent.state}, progress={torrent.progress}"
                )

            FILE_SELECTIONS.pop(torrent_hash, None)

            await query.answer("▶️ Download started!")

            await query.edit_message_text(
                "▶️ <b>Download started!</b>\n\n"
                f"📁 Selected files: {len(selected_indexes)}\n"
                f"📦 Torrent: {torrent.name if torrent else torrent_hash}",
                parse_mode="HTML",
            )

        except Exception as e:
            print(f"❌ File start error: {type(e).__name__}: {e}")

            try:
                await query.answer(
                    f"Error: {e}",
                    show_alert=True,
                )
            except Exception:
                pass

        return

    if query.data.startswith("filecancel:"):
        _, torrent_hash = query.data.split(":", 1)

        FILE_SELECTIONS.pop(torrent_hash, None)

        try:
            qb = get_qbittorrent()
            qb.torrents_delete(
                torrent_hashes=torrent_hash,
                delete_files=False,
            )
        except Exception as e:
            print(f"Cancel torrent error: {e}")

        await query.edit_message_text(
            "❌ Torrent cancelled.\n\n"
            "The torrent was removed and downloaded files were kept.",
        )

        return

    # Main menu buttons
    if query.data.startswith("menu_"):
        action = query.data
        screen_key = f"{query.message.chat_id}:{query.message.message_id}"

        if action != "menu_list":
            SCREEN_STATES[screen_key] = "menu"

        if action == "menu_home":
            await query.edit_message_text(
                "🤖 <b>Telegram Torrent Bot</b>\n\n"
                "Choose an option:",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard(),
            )
            return

        if action == "menu_list":
            qb = get_qbittorrent()
            status_text, torrents = await build_torrent_status(qb)

            await query.edit_message_text(
                status_text,
                parse_mode="HTML",
                reply_markup=list_keyboard(torrents),
            )

            start_torrent_list_refresh(
                context.application,
                query.message.chat_id,
                query.message.message_id,
            )
            return

        if action == "menu_space":
            total, used, free = shutil.disk_usage("/")

            def fmt_gb(value):
                return f"{value / (1024 ** 3):.2f} GB"

            downloads_size = 0

            for path in DOWNLOAD_DIR.rglob("*"):
                try:
                    if path.is_file():
                        downloads_size += path.stat().st_size
                except (OSError, FileNotFoundError):
                    pass

            used_percent = (used / total * 100) if total else 0

            filled = int(used_percent / 5)
            bar = "█" * filled + "░" * (20 - filled)

            text = (
                "💾 <b>Server Storage</b>\n\n"
                f"💿 Total: <b>{fmt_gb(total)}</b>\n"
                f"📦 Used: <b>{fmt_gb(used)}</b>\n"
                f"🟢 Free: <b>{fmt_gb(free)}</b>\n\n"
                f"📥 Downloads: <b>{fmt_gb(downloads_size)}</b>\n\n"
                f"{bar} <b>{used_percent:.1f}%</b>"
            )

            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🏠 Menu",
                            callback_data="menu_home",
                        )
                    ]
                ]),
            )
            return

        if action == "menu_status":
            try:
                qb = get_qbittorrent()
                torrents = qb.torrents_info()

                downloading = 0
                completed = 0
                stopped = 0
                total_download_speed = 0

                for torrent in torrents:
                    if torrent.progress >= 1:
                        completed += 1
                    elif torrent.state in (
                        "stoppedDL",
                        "stoppedUP",
                        "pausedDL",
                        "pausedUP",
                    ):
                        stopped += 1
                    else:
                        downloading += 1

                    total_download_speed += getattr(
                        torrent, "dlspeed", 0
                    )

                text = (
                    "📊 <b>Server / Torrent Status</b>\n\n"
                    f"📦 Total torrents: <b>{len(torrents)}</b>\n"
                    f"📥 Downloading: <b>{downloading}</b>\n"
                    f"✅ Completed: <b>{completed}</b>\n"
                    f"⏹ Stopped: <b>{stopped}</b>\n\n"
                    f"⬇️ Total download speed: "
                    f"<b>{format_speed(total_download_speed)}</b>"
                )

                await query.edit_message_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "🏠 Menu",
                                callback_data="menu_home",
                            )
                        ]
                    ]),
                )

            except Exception as e:
                print(f"Status menu error: {e}")
                await query.edit_message_text(
                    "❌ Failed to retrieve server status.",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "🏠 Menu",
                                callback_data="menu_home",
                            )
                        ]
                    ]),
                )

            return

        if action == "menu_help":
            await query.edit_message_text(
                "❓ <b>Help</b>\n\n"
                "📥 Paste a magnet link directly to add a torrent.\n\n"
                "/list — View and manage torrents\n"
                "/space — View server storage\n"
                "/test — Test qBittorrent connection\n"
                "/link HASH — Generate a file link\n"
                "/stop HASH — Stop a torrent\n"
                "/starttorrent HASH — Resume a torrent\n"
                "/delete HASH — Delete torrent and files\n"
                "/menu — Open this control panel",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🏠 Menu",
                            callback_data="menu_home",
                        )
                    ]
                ]),
            )
            return

        if action == "menu_add":
            await query.edit_message_text(
                "📥 <b>Add Torrent</b>\n\n"
                "Send a magnet link directly in this chat.\n\n"
                "Example:\n"
                "<code>magnet:?xt=urn:btih:...</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🏠 Menu",
                            callback_data="menu_home",
                        )
                    ]
                ]),
            )
            return

    # Individual file progress
    if query.data.startswith("files:"):
        _, torrent_hash = query.data.split(":", 1)

        # This message is now officially on File Progress.
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        key = f"{chat_id}:{message_id}"

        SCREEN_STATES[key] = "file_progress"

        print(
            f"📁 File Progress screen active: "
            f"message={message_id}"
        )


        chat_id = query.message.chat_id
        message_id = query.message.message_id
        key = f"{chat_id}:{message_id}"

        try:
            qb = get_qbittorrent()

            text = await build_file_progress(
                qb,
                torrent_hash,
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data=f"files:{torrent_hash}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back to Torrents",
                        callback_data="menu_list",
                    )
                ],
            ])

            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )

            await query.answer()

            # ------------------------------------------------
            # Cancel previous File Progress task.
            # ------------------------------------------------

            old_task = FILE_PROGRESS_TASKS.pop(
                key,
                None,
            )

            if old_task:
                old_task.cancel()

            # ------------------------------------------------
            # Start exactly one File Progress task.
            # ------------------------------------------------

            task = asyncio.create_task(
                refresh_file_progress_message(
                    context.application,
                    chat_id,
                    message_id,
                    torrent_hash,
                )
            )

            FILE_PROGRESS_TASKS[key] = task

            print(
                f"📊 File Progress auto-refresh started "
                f"for message {message_id}"
            )

        except Exception as e:
            print(
                f"❌ File progress error: {e}"
            )

            await query.answer(
                "Failed to retrieve file progress.",
                show_alert=True,
            )

        return

    action, torrent_hash = query.data.split(":", 1)

    try:
        qb = get_qbittorrent()
        torrent = get_torrent(qb, torrent_hash)

        if not torrent:
            await query.message.reply_text("❌ Torrent not found.")
            return

        if action == "refresh":
            qb = get_qbittorrent()
            torrent = get_torrent(qb, torrent_hash)

            if not torrent:
                await query.message.reply_text(
                    "❌ Torrent not found."
                )
                return

            progress = torrent.progress * 100
            downloaded = getattr(torrent, "downloaded", 0)
            total_size = getattr(torrent, "size", 0)
            speed = getattr(torrent, "dlspeed", 0)

            if torrent.progress >= 1:
                status = "✅ Completed"
            elif torrent.state in ("downloading", "forcedDL"):
                status = "📥 Downloading"
            elif torrent.state in ("stoppedDL", "stoppedUP"):
                status = "⏹ Stopped"
            elif torrent.state == "metaDL":
                status = "🔎 Getting metadata"
            else:
                status = f"📡 {torrent.state}"

            text = (
                f"📦 <b>{torrent.name}</b>\n\n"
                f"📊 Progress: <b>{progress:.1f}%</b>\n"
                f"💾 {format_size(downloaded)} / "
                f"{format_size(total_size)}\n"
                f"⬇️ Speed: {format_speed(speed)}\n"
                f"📡 Status: {status}\n\n"
                f"🔑 <code>{torrent.hash}</code>"
            )

            await query.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=torrent_buttons(torrent.hash),
            )
            return

        if action == "stop":
            qb.torrents_stop(torrent_hashes=torrent_hash)
            await query.message.reply_text(
                f"⏹ Stopped:\n{torrent.name}"
            )

        elif action == "start":
            qb.torrents_start(torrent_hashes=torrent_hash)
            await query.message.reply_text(
                f"▶️ Started:\n{torrent.name}"
            )

        elif action == "delete":
            name = torrent.name

            qb.torrents_delete(
                torrent_hashes=torrent_hash,
                delete_files=True,
            )

            await query.message.reply_text(
                f"🗑 Deleted torrent and files:\n\n{name}"
            )

        elif action == "link":
            if torrent.progress < 1:
                await query.message.reply_text(
                    "⏳ Torrent is not complete yet."
                )
                return

            await create_links(query.message, torrent_hash)

    except Exception as e:
        print(f"Button error: {e}")
        await query.message.reply_text(
            f"❌ Operation failed:\n{e}"
        )


async def completion_watcher(application):
    """
    Watch qBittorrent for completed torrents and automatically
    send direct download links.

    The completion state is persisted in .completed_torrents.json
    so the same torrent is not announced repeatedly after restart.
    """
    print("🔎 Completion watcher started...")

    while True:
        try:
            await asyncio.sleep(5)

            qb = get_qbittorrent()
            torrents = qb.torrents_info()

            for torrent in torrents:
                torrent_hash = torrent.hash

                if torrent.progress < 1:
                    continue

                # Already announced.
                if torrent_hash in COMPLETED_TORRENTS:
                    continue

                print(f"🎉 Torrent completed: {torrent.name}")

                try:
                    files = qb.torrents_files(torrent_hash=torrent_hash)

                    if not files:
                        print(f"⚠️ No files found for: {torrent.name}")
                        COMPLETED_TORRENTS.add(torrent_hash)
                        save_completed_torrents(COMPLETED_TORRENTS)
                        continue

                    links = []

                    for file in files:
                        file_name = getattr(file, "name", None)

                        if not file_name:
                            continue

                        # qBittorrent's content path is normally under
                        # /downloads for this setup.
                        file_path = (DOWNLOAD_DIR / file_name).resolve()

                        try:
                            file_path.relative_to(DOWNLOAD_DIR)
                        except ValueError:
                            print(f"⚠️ Skipping unsafe path: {file_path}")
                            continue

                        if not file_path.is_file():
                            print(f"⚠️ File not found yet: {file_path}")
                            continue

                        token = secrets.token_urlsafe(24)
                        save_token(token, file_path)

                        url = (
                            f"{BASE_URL}/files/"
                            f"{token}/"
                            f"{quote(file_path.name)}"
                        )

                        links.append((file_path.name, url))

                    if not links:
                        print(f"⚠️ No completed files available: {torrent.name}")
                        continue

                    text = (
                        f"🎉 <b>Download complete!</b>\n\n"
                        f"📦 <b>{torrent.name}</b>\n\n"
                    )

                    for index, (name, url) in enumerate(links, 1):
                        text += (
                            f"📄 <b>{name}</b>\n"
                            f"🔗 {url}\n\n"
                        )

                    # Send to the authorized user.
                    await application.bot.send_message(
                        chat_id=ALLOWED_USER_ID,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                    COMPLETED_TORRENTS.add(torrent_hash)
                    save_completed_torrents(COMPLETED_TORRENTS)

                    print(f"✅ Completion notification sent: {torrent.name}")

                except Exception as e:
                    print(
                        f"❌ Completion handling error "
                        f"for {torrent.name}: {e}"
                    )

        except asyncio.CancelledError:
            print("Completion watcher stopped.")
            return

        except Exception as e:
            print(f"Completion watcher error: {e}")



async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("menu", "Open control panel"),
        BotCommand("list", "View all torrents"),
        BotCommand("space", "Check server storage"),
        BotCommand("test", "Test qBittorrent connection"),
        BotCommand("help", "Show help"),
        BotCommand("link", "Get a file link"),
        BotCommand("stop", "Stop a torrent"),
        BotCommand("starttorrent", "Resume a torrent"),
        BotCommand("delete", "Delete a torrent"),
    ]

    await application.bot.set_my_commands(commands)
    print("✅ Telegram command menu configured.")



async def post_init(application):
    global TELEGRAM_APPLICATION

    TELEGRAM_APPLICATION = application

    await setup_bot_commands(application)
    asyncio.create_task(completion_watcher(application))



async def space_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show server disk space and /downloads usage."""
    try:
        total, used, free = shutil.disk_usage("/")

        def fmt_gb(value):
            return f"{value / (1024 ** 3):.2f} GB"

        # Calculate downloads directory size.
        downloads_size = 0

        for path in DOWNLOAD_DIR.rglob("*"):
            try:
                if path.is_file():
                    downloads_size += path.stat().st_size
            except (OSError, FileNotFoundError):
                pass

        used_percent = (used / total * 100) if total else 0

        # 20-character progress bar.
        filled = int(used_percent / 5)
        bar = "█" * filled + "░" * (20 - filled)

        text = (
            "💾 <b>Server Storage</b>\n\n"
            f"💿 Total: <b>{fmt_gb(total)}</b>\n"
            f"📦 Used: <b>{fmt_gb(used)}</b>\n"
            f"🟢 Free: <b>{fmt_gb(free)}</b>\n\n"
            f"📥 Downloads: <b>{fmt_gb(downloads_size)}</b>\n\n"
            f"{bar} <b>{used_percent:.1f}%</b>"
        )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
        )

    except Exception as e:
        print(f"Space error: {e}")
        await update.message.reply_text(
            "❌ Failed to check server storage."
        )



def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Add Torrent", callback_data="menu_add"),
            InlineKeyboardButton("📋 My Torrents", callback_data="menu_list"),
        ],
        [
            InlineKeyboardButton("💾 Storage", callback_data="menu_space"),
            InlineKeyboardButton("📊 Server Status", callback_data="menu_status"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help"),
        ],
    ])


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return

    await update.message.reply_text(
        "🤖 <b>Telegram Torrent Bot</b>\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )



def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
                .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("test", test_qbittorrent))
    app.add_handler(CommandHandler("magnet", add_magnet))
    app.add_handler(CommandHandler("list", list_torrents))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("space", space_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("stop", stop_torrent))
    app.add_handler(CommandHandler("starttorrent", start_torrent))
    app.add_handler(CommandHandler("delete", delete_torrent))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    print("🤖 Telegram Torrent Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
