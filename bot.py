"""Telegram bot that downloads public TikTok videos sent as links."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

load_dotenv()

# Bothost can expose the Telegram token under TELEGRAM_BOT_TOKEN;
# local development uses BOT_TOKEN from .env.
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
PROXY_URL = os.getenv("PROXY_URL")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "49")) * 1024 * 1024
TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

dp = Dispatcher()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def get_tiktok_url(text: str) -> Optional[str]:
    """Return the first TikTok URL from a Telegram message."""
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?)]]}")
    host = (urlparse(url).hostname or "").lower()
    return url if host in TIKTOK_HOSTS else None


def download_video(url: str, destination: Path) -> Tuple[Path, str]:
    """Download a single video and return its path and title."""
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
        video_path = Path(ydl.prepare_filename(info))
        if video_path.suffix.lower() != ".mp4":
            mp4_path = video_path.with_suffix(".mp4")
            if mp4_path.exists():
                video_path = mp4_path
        if not video_path.exists():
            candidates = list(destination.glob(f"{info['id']}.*"))
            if not candidates:
                raise FileNotFoundError("yt-dlp did not create a video file")
            video_path = max(candidates, key=lambda item: item.stat().st_size)
        if video_path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("video_too_large")
        return video_path, info.get("title") or "TikTok video"


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Отправьте ссылку на публичное видео из TikTok — я скачаю его и пришлю файлом."
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Пришлите ссылку вида https://www.tiktok.com/@.../video/...\n"
        "Поддерживаются публичные ролики; максимум — 49 МБ."
    )


@dp.message(F.text)
async def handle_link(message: Message) -> None:
    url = get_tiktok_url(message.text or "")
    if not url:
        await message.answer("Пришлите корректную ссылку на видео TikTok.")
        return

    status = await message.answer("Скачиваю видео…")
    tmp_dir = Path(tempfile.mkdtemp(prefix="tiktok_bot_"))
    try:
        video_path, title = await asyncio.to_thread(download_video, url, tmp_dir)
        await status.edit_text("Отправляю файл…")
        await message.answer_document(
            FSInputFile(video_path, filename=f"{title[:80]}.mp4"),
            caption="Готово.",
        )
        await status.delete()
    except ValueError as exc:
        if str(exc) == "video_too_large":
            await status.edit_text("Видео слишком большое для отправки ботом (лимит 49 МБ).")
        else:
            logger.exception("Unexpected value error")
            await status.edit_text("Не получилось обработать видео.")
    except (DownloadError, FileNotFoundError):
        logger.info("Download failed for %s", url, exc_info=True)
        await status.edit_text(
            "Не удалось скачать ролик. Проверьте, что он публичный и ссылка рабочая."
        )
    except Exception:
        logger.exception("Unhandled error while processing TikTok URL")
        await status.edit_text("Внутренняя ошибка. Попробуйте ещё раз позже.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def main() -> None:
    if not TOKEN or TOKEN == "123456789:replace_with_your_token":
        raise RuntimeError("Set BOT_TOKEN in the .env file before starting the bot.")
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
