"""Private Telegram bot: personal bookkeeping and public video downloads."""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv
from PIL import Image
from pypdf import PdfReader
import pytesseract
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
PROXY_URL = os.getenv("PROXY_URL")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "49")) * 1024 * 1024
GIFT_SEARCH_LIMIT = max(1, min(int(os.getenv("GIFT_SEARCH_LIMIT", "10")), 10))
GIFT_SEARCH_QUERY = os.getenv("GIFT_SEARCH_QUERY", "телеграм подарки Telegram Gifts")
GIFT_CONTENT_INTERVAL_HOURS = max(0, int(os.getenv("GIFT_CONTENT_INTERVAL_HOURS", "0")))
ALLOWED_USERNAME = os.getenv("ALLOWED_USERNAME", "temachooo").lstrip("@").lower()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = DATA_DIR / "finance.sqlite3"
PUBLISH_QUEUE_DIR = DATA_DIR / "publication_queue"
VIDEO_HOSTS = {
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
}
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
ENTRY_RE = re.compile(
    r"^\s*(?:(?P<sign>[+-])\s*|(?P<word>доход|расход)\s+)(?P<amount>[\d\s.,]+)(?:\s*(?:₽|руб\.?))?(?:\s+(?P<note>.*))?$",
    re.IGNORECASE,
)
MONEY_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[,.](\d{1,2}))?(?!\d)")
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]{3,}")
STOP_WORDS = {
    "это", "как", "для", "про", "или", "что", "the", "with", "from", "and",
    "telegram", "телеграм", "gifts", "подарки", "gift", "video", "видео",
}
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📤 Опубликовать видео")]],
    resize_keyboard=True,
)

dp = Dispatcher()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def init_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLISH_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount_kopecks INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                proof_file_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS publication_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_file_id TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS delivered_gift_content (
                video_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS content_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                creator TEXT NOT NULL DEFAULT '',
                search_query TEXT NOT NULL,
                telegram_unique_id TEXT,
                delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS content_selections (
                candidate_id INTEGER PRIMARY KEY,
                selected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(candidate_id) REFERENCES content_candidates(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_actions (
                chat_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )"""
        )


def is_owner(message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    if ALLOWED_USER_ID:
        return user.id == ALLOWED_USER_ID
    return (user.username or "").lower() == ALLOWED_USERNAME


async def reject_if_not_owner(message: Message) -> bool:
    if is_owner(message):
        # Keep the private chat id so scheduled ideas can be delivered even
        # while the owner is offline and the bot is running on the server.
        if message.chat.type == "private":
            save_setting("owner_chat_id", str(message.chat.id))
        return False
    await message.answer("Этот бот — личный и доступен только владельцу.")
    return True


def parse_money(raw_amount: str) -> int:
    normalized = raw_amount.replace(" ", "").replace(",", ".")
    value = float(normalized)
    if value <= 0:
        raise ValueError("Amount must be positive")
    return round(value * 100)


def parse_entry(text: str) -> Optional[Tuple[int, str]]:
    match = ENTRY_RE.match(text or "")
    if not match:
        return None
    amount = parse_money(match.group("amount"))
    is_expense = match.group("sign") == "-" or (match.group("word") or "").lower() == "расход"
    return (-amount if is_expense else amount, (match.group("note") or "").strip())


def direction_from_text(text: str) -> Optional[int]:
    lowered = (text or "").lower()
    if re.search(r"(^|\s)\+|доход|зачислен|поступлен|получен|входящ|пополнен", lowered):
        return 1
    if re.search(r"(^|\s)-|расход|списан|оплат[аы]|перевод на|выдан", lowered):
        return -1
    return None


def amount_from_receipt(text: str) -> Optional[int]:
    """Pick the most likely total from OCR/PDF text without guessing direction."""
    candidates = []
    for match in MONEY_RE.finditer(text):
        rubles = int(match.group(1).replace(" ", "").replace("\u00a0", ""))
        kopecks = int((match.group(2) or "0").ljust(2, "0"))
        value = rubles * 100 + kopecks
        # Ignore years, short codes and unrealistically large values.
        if 100 <= value <= 10_000_000_000:
            context = text[max(0, match.start() - 45):match.end() + 20].lower()
            score = value
            if any(word in context for word in ("итого", "сумма", "перевод", "операци", "зачислен", "списан", "оплат")):
                score += 1_000_000_000_000
            candidates.append((score, value))
    return max(candidates)[1] if candidates else None


def ocr_image(path: Path) -> str:
    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang="rus+eng")


def extract_pdf_text(path: Path) -> str:
    text_parts = []
    try:
        reader = PdfReader(str(path))
        text_parts.extend(page.extract_text() or "" for page in reader.pages[:5])
    except Exception:
        logger.info("PDF text extraction failed", exc_info=True)
    text = "\n".join(text_parts).strip()
    if text:
        return text

    # Scanned PDF: render the first two pages and recognize them as images.
    image_prefix = path.with_suffix("")
    subprocess.run(
        ["pdftoppm", "-f", "1", "-l", "2", "-jpeg", str(path), str(image_prefix)],
        check=True, capture_output=True, timeout=30,
    )
    pages = sorted(path.parent.glob(f"{image_prefix.name}-*.jpg"))
    return "\n".join(ocr_image(page) for page in pages)


async def save_receipt_from_text(message: Message, receipt_text: str, proof_file_id: str) -> None:
    amount = amount_from_receipt(receipt_text)
    direction = direction_from_text((message.caption or "") + "\n" + receipt_text)
    if not amount:
        await message.answer("Не смог прочитать сумму. Пришлите более чёткий скрин или добавьте сумму в подпись.")
        return
    if not direction:
        await message.answer(
            f"Нашёл сумму {format_money(amount)}. Не понял: это доход или расход. "
            "Пришлите этот чек ещё раз с подписью `доход` или `расход`.",
            parse_mode="Markdown",
        )
        return
    note = "чек" if not message.caption else message.caption.strip()
    add_entry(amount * direction, note, proof_file_id)
    kind = "доход" if direction > 0 else "расход"
    await message.answer(f"Распознал и записал {kind}: {format_money(amount)}.")


def format_money(kopecks: int) -> str:
    prefix = "−" if kopecks < 0 else ""
    absolute = abs(kopecks)
    return f"{prefix}{absolute // 100:,}.{absolute % 100:02d} ₽".replace(",", " ")


def add_entry(amount: int, note: str, proof_file_id: Optional[str] = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO entries (amount_kopecks, note, proof_file_id) VALUES (?, ?, ?)",
            (amount, note, proof_file_id),
        )


def add_to_publication_queue(
    telegram_file_id: str, stored_path: Path, original_name: str, caption: str
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """INSERT INTO publication_queue
               (telegram_file_id, stored_path, original_name, caption)
               VALUES (?, ?, ?, ?)""",
            (telegram_file_id, str(stored_path), original_name, caption),
        )
        return int(cursor.lastrowid)


def publication_queue_report() -> str:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT id, original_name, caption, created_at
               FROM publication_queue
               WHERE status = 'ready'
               ORDER BY id DESC LIMIT 20"""
        ).fetchall()
    if not rows:
        return "Очередь публикаций пуста. Пришлите боту готовое видео."
    lines = ["🎬 Очередь TikTok-публикаций:"]
    for queue_id, filename, caption, created_at in rows:
        caption_text = f" — {caption[:50]}" if caption else ""
        lines.append(f"№{queue_id}: {filename}{caption_text} ({created_at[:16]})")
    lines.append(
        "\nДля публикации в TikTok пришлите нужный видеофайл в чат Codex: "
        "сервер бота не передаёт файлы в браузер автоматически."
    )
    return "\n".join(lines)


def already_delivered(video_key: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT 1 FROM delivered_gift_content WHERE video_key = ?", (video_key,)
        ).fetchone() is not None


def remember_delivered(video_key: str, title: str, source_url: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO delivered_gift_content (video_key, title, source_url)
               VALUES (?, ?, ?)""",
            (video_key, title, source_url),
        )


def tokenize(text: str) -> set[str]:
    return {
        word.lower()
        for word in WORD_RE.findall(text or "")
        if word.lower() not in STOP_WORDS
    }


def preference_weights() -> dict[str, int]:
    """Build a small transparent preference profile from chosen examples."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT c.title, c.creator
               FROM content_selections s
               JOIN content_candidates c ON c.id = s.candidate_id
               ORDER BY s.selected_at DESC LIMIT 50"""
        ).fetchall()
    weights: dict[str, int] = {}
    # More recent selections have a little more influence.
    for position, (title, creator) in enumerate(rows):
        weight = max(1, 6 - position // 10)
        for token in tokenize(f"{title} {creator}"):
            weights[token] = weights.get(token, 0) + weight
    return weights


def rank_candidates(
    candidates: list[tuple[str, str, str, str]], query: str
) -> list[tuple[str, str, str, str]]:
    query_tokens = tokenize(query)
    preferences = preference_weights()

    def score(candidate: tuple[str, str, str, str]) -> int:
        _video_key, _url, title, creator = candidate
        tokens = tokenize(f"{title} {creator}")
        return len(tokens & query_tokens) * 20 + sum(preferences.get(token, 0) for token in tokens)

    return sorted(candidates, key=score, reverse=True)


def save_content_candidate(
    video_key: str,
    title: str,
    source_url: str,
    creator: str,
    query: str,
    telegram_unique_id: str,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO content_candidates
               (video_key, title, source_url, creator, search_query, telegram_unique_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (video_key, title, source_url, creator, query, telegram_unique_id),
        )


def select_candidate(telegram_unique_id: str) -> Optional[str]:
    """Save an explicit choice and return its title, or None for an unknown video."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT id, title FROM content_candidates
               WHERE telegram_unique_id = ?""",
            (telegram_unique_id,),
        ).fetchone()
        if not row:
            return None
        candidate_id, title = row
        conn.execute(
            "INSERT OR IGNORE INTO content_selections (candidate_id) VALUES (?)",
            (candidate_id,),
        )
    return str(title)


def set_pending_action(chat_id: int, action: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO pending_actions (chat_id, action) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   action = excluded.action,
                   created_at = CURRENT_TIMESTAMP""",
            (chat_id, action),
        )


def consume_pending_action(chat_id: int, action: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT action FROM pending_actions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if not row or row[0] != action:
            return False
        conn.execute("DELETE FROM pending_actions WHERE chat_id = ?", (chat_id,))
        return True


def save_setting(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO bot_settings (setting_key, setting_value) VALUES (?, ?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (key, value),
        )


def get_setting(key: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,)
        ).fetchone()
    return str(row[0]) if row else None


def report() -> str:
    with sqlite3.connect(DB_PATH) as conn:
        income, expense, balance, count = conn.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN amount_kopecks > 0 THEN amount_kopecks ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN amount_kopecks < 0 THEN amount_kopecks ELSE 0 END), 0),
                COALESCE(SUM(amount_kopecks), 0), COUNT(*)
            FROM entries"""
        ).fetchone()
        latest = conn.execute(
            "SELECT amount_kopecks, note, created_at FROM entries ORDER BY id DESC LIMIT 5"
        ).fetchall()

    lines = [
        "📊 Общий отчёт",
        f"Доходы: {format_money(income)}",
        f"Расходы: {format_money(expense)}",
        f"Баланс: {format_money(balance)}",
        f"Операций: {count}",
    ]
    if latest:
        lines.append("\nПоследние операции:")
        for amount, note, created_at in latest:
            sign = "➕" if amount > 0 else "➖"
            description = f" — {note}" if note else ""
            lines.append(f"{sign} {format_money(amount)}{description} ({created_at[:10]})")
    return "\n".join(lines)


def get_supported_url(text: str) -> Optional[str]:
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?)]]}")
    host = (urlparse(url).hostname or "").lower()
    return url if host in VIDEO_HOSTS else None


def safe_filename(title: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|\n\r]+', "_", title).strip(" ._")
    return (clean or "video")[:80] + ".mp4"


def download_video(url: str, destination: Path) -> Tuple[Path, str]:
    options = {
        "outtmpl": str(destination / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_FILE_SIZE,
        "socket_timeout": 30,
        "retries": 2,
    }
    if PROXY_URL:
        options["proxy"] = PROXY_URL
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        candidates = list(destination.glob(f"{info['id']}.*"))
        if not candidates:
            raise FileNotFoundError("yt-dlp did not create a video file")
        video_path = max(candidates, key=lambda item: item.stat().st_size)
        if video_path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("video_too_large")
        return video_path, info.get("title") or "video"


def search_gift_videos(query: str, limit: int) -> list[tuple[str, str, str, str]]:
    """Return public YouTube videos for the requested gift topic.

    Searching YouTube is intentionally used here: TikTok does not provide a
    dependable public search endpoint for a personal bot. The returned URLs
    still go through the normal downloader and are sent as files, not links.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "discard_in_playlist",
        "noplaylist": True,
    }
    with YoutubeDL(options) as ydl:
        result = ydl.extract_info(f"ytsearch{max(limit * 3, 30)}:{query}", download=False)
    videos = []
    for entry in result.get("entries", []):
        if not entry:
            continue
        video_id = str(entry.get("id") or "")
        webpage_url = entry.get("webpage_url") or entry.get("url")
        if not video_id or not webpage_url:
            continue
        videos.append(
            (
                video_id,
                str(webpage_url),
                str(entry.get("title") or "video"),
                str(entry.get("channel") or entry.get("uploader") or ""),
            )
        )
    return rank_candidates(videos, query)


async def deliver_gift_content(bot: Bot, chat_id: int, query: str) -> int:
    """Download and deliver previously unseen public videos as Telegram files."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="gift_content_"))
    sent = 0
    try:
        candidates = await asyncio.to_thread(search_gift_videos, query, GIFT_SEARCH_LIMIT)
        for video_key, url, title, creator in candidates:
            if sent >= GIFT_SEARCH_LIMIT:
                break
            if already_delivered(video_key):
                continue
            try:
                video_path, downloaded_title = await asyncio.to_thread(download_video, url, tmp_dir)
                delivered_message = await bot.send_video(
                    chat_id,
                    FSInputFile(video_path, filename=safe_filename(downloaded_title)),
                    caption=f"🎁 Идея {sent + 1}/{GIFT_SEARCH_LIMIT}: {downloaded_title}\n"
                    "Перешли этот ролик обратно боту, если он подходит.",
                )
                remember_delivered(video_key, downloaded_title, url)
                if delivered_message.video:
                    save_content_candidate(
                        video_key,
                        downloaded_title,
                        url,
                        creator,
                        query,
                        delivered_message.video.file_unique_id,
                    )
                sent += 1
            except (DownloadError, FileNotFoundError, ValueError):
                logger.info("Gift content download skipped: %s", url, exc_info=True)
        return sent
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def scheduled_gift_content_loop(bot: Bot) -> None:
    """Send fresh ideas periodically; disabled when the interval is 0."""
    if not GIFT_CONTENT_INTERVAL_HOURS:
        return
    interval = GIFT_CONTENT_INTERVAL_HOURS * 60 * 60
    while True:
        await asyncio.sleep(interval)
        chat_id = get_setting("owner_chat_id")
        if not chat_id:
            logger.info("Scheduled gift ideas skipped: owner has not used /start yet")
            continue
        try:
            sent = await deliver_gift_content(bot, int(chat_id), GIFT_SEARCH_QUERY)
            logger.info("Scheduled gift ideas sent: %s", sent)
        except Exception:
            logger.exception("Scheduled gift ideas failed")


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    await message.answer(
        "Личный бот готов.\n\n"
        "Учёт: `+ 1500 доход` или `- 300 расход`\n"
        "Скрин оплаты: приложите фото с такой же подписью.\n"
        "Отчёт: /report\n"
        "Видео: пришлите ссылку TikTok или YouTube на публичный ролик.\n\n"
        "Подбор контента: /gifts или /gifts ваша тема — пришлю 10 видео.\n"
        "Перешлите понравившееся видео обратно — запомню ваш выбор.\n\n"
        f"Ваш Telegram ID: `{message.from_user.id}`",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.message(Command("report"))
async def report_command(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    await message.answer(report())


@dp.message(Command("queue"))
async def queue_command(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    await message.answer(publication_queue_report())


@dp.message(F.text == "📤 Опубликовать видео")
async def publish_button(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    set_pending_action(message.chat.id, "publish_video")
    await message.answer(
        "Пришли следующим сообщением готовый видеофайл. "
        "Я сохраню его в очередь публикации."
    )


@dp.message(Command("gifts"))
async def gifts_command(message: Message) -> None:
    """Find a few new public videos about Telegram Gifts and send files."""
    if await reject_if_not_owner(message):
        return
    requested_topic = (message.text or "").partition(" ")[2].strip()
    query = requested_topic or GIFT_SEARCH_QUERY
    status = await message.answer("Ищу свежие публичные ролики про Telegram Gifts…")
    try:
        sent = await deliver_gift_content(message.bot, message.chat.id, query)
        if sent:
            await status.edit_text(f"Отправил {sent} новых видеофайла(ов).")
        else:
            await status.edit_text(
                "Новых роликов сейчас не нашёл. Попробуйте позже или уточните тему: "
                "`/gifts NFT подарки`.",
                parse_mode="Markdown",
            )
    except Exception:
        logger.exception("Gift content search failed")
        await status.edit_text("Не получилось найти ролики сейчас. Попробуйте ещё раз позже.")


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    await message.answer(
        "Команды:\n"
        "• `+ 1500 продажа` — доход\n"
        "• `- 300 такси` — расход\n"
        "• фото с подписью `+ 1500 перевод` — запись со скрином\n"
        "• /report — сводка\n"
        "• TikTok/YouTube-ссылка — скачать публичное видео\n"
        "• /gifts или `/gifts NFT подарки` — получить 10 подходящих видео\n"
        "• перешлите понравившийся ролик обратно — бот учтёт его как предпочтение",
        parse_mode="Markdown",
    )


@dp.message(F.photo)
async def handle_receipt(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    entry = parse_entry(message.caption or "")
    if entry:
        amount, note = entry
        add_entry(amount, note, message.photo[-1].file_id)
        await message.answer(f"Записал: {format_money(amount)}" + (f" — {note}" if note else ""))
        return
    status = await message.answer("Читаю скрин чека…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="receipt_ocr_"))
    try:
        image_path = tmp_dir / "receipt.jpg"
        await message.bot.download(message.photo[-1], destination=image_path)
        receipt_text = await asyncio.to_thread(ocr_image, image_path)
        await save_receipt_from_text(message, receipt_text, message.photo[-1].file_id)
        await status.delete()
    except Exception:
        logger.exception("Receipt OCR failed")
        await status.edit_text("Не смог прочитать этот скрин. Добавьте подпись с суммой: `+ 1500 доход`.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dp.message(F.video)
async def handle_ready_video(message: Message) -> None:
    """Treat a returned recommendation as feedback, not as a publish request."""
    if await reject_if_not_owner(message):
        return
    video = message.video
    if consume_pending_action(message.chat.id, "publish_video"):
        if video.file_size and video.file_size > MAX_FILE_SIZE:
            await message.answer(
                "Этот ролик больше лимита загрузки бота. Сожмите его до "
                f"{MAX_FILE_SIZE // 1024 // 1024} МБ и нажмите кнопку ещё раз."
            )
            return
        status = await message.answer("Сохраняю видео в очередь публикации…")
        suffix = Path(video.file_name or "video.mp4").suffix.lower() or ".mp4"
        filename = safe_filename(Path(video.file_name or "video").stem)
        if suffix != ".mp4":
            filename = f"{Path(filename).stem}{suffix}"
        try:
            temp_path = PUBLISH_QUEUE_DIR / f"upload_{message.message_id}{suffix}.part"
            await message.bot.download(video, destination=temp_path)
            final_path = PUBLISH_QUEUE_DIR / f"{message.message_id}_{filename}"
            temp_path.replace(final_path)
            queue_id = add_to_publication_queue(
                video.file_id, final_path, filename, (message.caption or "").strip()
            )
            await status.edit_text(
                f"🎬 Видео №{queue_id} принято для публикации.\n"
                "Оно сохранено на сервере. Сам бот не может законно публиковать "
                "его в TikTok без отдельного подтверждения в TikTok."
            )
        except Exception:
            logger.exception("Ready video queueing failed")
            await status.edit_text("Не удалось сохранить видео. Нажми кнопку и отправь его ещё раз.")
        finally:
            for partial in PUBLISH_QUEUE_DIR.glob("*.part"):
                partial.unlink(missing_ok=True)
        return
    title = select_candidate(video.file_unique_id)
    if title:
        await message.answer(
            f"👍 Запомнил выбор: {title}\n"
            "Следующие 10 вариантов буду ранжировать с учётом таких тем и авторов."
        )
        return
    await message.answer(
        "Не смог связать это видео с ранее присланной подборкой. "
        "Перешли его именно из сообщения бота, не скачивай и не загружай заново."
    )


@dp.message(F.document)
async def handle_pdf_receipt(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    document = message.document
    filename = (document.file_name or "").lower()
    if not filename.endswith(".pdf"):
        await message.answer("Для учёта пришлите фото или PDF-чек.")
        return
    status = await message.answer("Читаю PDF-чек…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="receipt_pdf_"))
    try:
        pdf_path = tmp_dir / "receipt.pdf"
        await message.bot.download(document, destination=pdf_path)
        receipt_text = await asyncio.to_thread(extract_pdf_text, pdf_path)
        await save_receipt_from_text(message, receipt_text, document.file_id)
        await status.delete()
    except Exception:
        logger.exception("PDF receipt extraction failed")
        await status.edit_text("Не смог прочитать PDF. Добавьте к нему подпись: `+ 1500 доход`.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    if await reject_if_not_owner(message):
        return
    entry = parse_entry(message.text or "")
    if entry:
        amount, note = entry
        add_entry(amount, note)
        await message.answer(f"Записал: {format_money(amount)}" + (f" — {note}" if note else ""))
        return

    url = get_supported_url(message.text or "")
    if not url:
        await message.answer("Пришлите TikTok/YouTube-ссылку или запись вида `+ 1500 доход`.", parse_mode="Markdown")
        return

    status = await message.answer("Скачиваю видео…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="media_bot_"))
    try:
        video_path, title = await asyncio.to_thread(download_video, url, tmp_dir)
        await status.edit_text("Отправляю файл…")
        await message.answer_video(FSInputFile(video_path, filename=safe_filename(title)), caption="Готово.")
        await status.delete()
    except ValueError as exc:
        if str(exc) == "video_too_large":
            await status.edit_text("Видео слишком большое для отправки ботом (лимит 49 МБ).")
        else:
            logger.exception("Unexpected value error")
            await status.edit_text("Не получилось обработать видео.")
    except (DownloadError, FileNotFoundError):
        logger.info("Download failed for %s", url, exc_info=True)
        await status.edit_text("Не удалось скачать ролик. Проверьте ссылку и доступ к видео.")
    except Exception:
        logger.exception("Unhandled error while processing video URL")
        await status.edit_text("Внутренняя ошибка. Попробуйте ещё раз позже.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def main() -> None:
    if not TOKEN or TOKEN == "123456789:replace_with_your_token":
        raise RuntimeError("Set BOT_TOKEN before starting the bot.")
    init_database()
    bot = Bot(TOKEN)
    scheduled_task = asyncio.create_task(scheduled_gift_content_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        scheduled_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduled_task


if __name__ == "__main__":
    asyncio.run(main())
