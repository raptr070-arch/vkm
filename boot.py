import asyncio
import os
import re
import hashlib
import json
import time
import subprocess
import signal
import sys
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, FSInputFile
)
from aiogram.filters import CommandStart, Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
import logging
import aiohttp

# Shazam (ixtiyoriy)
try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False

# =================== KONFIG ===================
load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DOWNLOADS_PATH = Path("/tmp/downloads")
    TEMP_PATH = Path("/tmp/temp_audio")
    MAX_FILE_SIZE = 50 * 1024 * 1024
    KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))
    PING_INTERVAL = 300  # 5 daqiqa

if not Config.BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

# Papkalar
Config.DOWNLOADS_PATH.mkdir(exist_ok=True)
Config.TEMP_PATH.mkdir(exist_ok=True)

# FFmpeg tekshiruvi
def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except:
        return False

FFMPEG_AVAILABLE = check_ffmpeg()

# Cookies (MUHIM!)
COOKIES_FILE = 'cookies.txt'
COOKIES_AVAILABLE = os.path.exists(COOKIES_FILE)
if COOKIES_AVAILABLE:
    print("✅ YouTube cookies topildi!")
else:
    print("⚠️ cookies.txt YO'Q! YouTube sign in xatosini chiqaradi.")

# Bot va dispatcher
session = AiohttpSession(timeout=60)
bot = Bot(token=Config.BOT_TOKEN, session=session,
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=2)  # Render uchun yetarli

# Vaqtinchalik ma'lumotlar
temp_data: Dict[str, dict] = {}
video_cache: Dict[str, dict] = {}
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True

# =================== YORDAMCHI FUNKSIYALAR ===================
def get_platform(url: str) -> str:
    url_lower = url.lower()
    if any(x in url_lower for x in ['youtube.com', 'youtu.be']):
        return 'youtube'
    elif any(x in url_lower for x in ['instagram.com', 'instagr.am']):
        return 'instagram'
    elif 'tiktok.com' in url_lower:
        return 'tiktok'
    elif any(x in url_lower for x in ['facebook.com', 'fb.watch']):
        return 'facebook'
    return 'other'

def format_duration(seconds):
    if not seconds:
        return "0:00"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}:{secs:02d}"

def format_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def extract_artist_title(full_title: str):
    if not full_title:
        return "", ""
    # "Artist - Title" yoki "Artist — Title"
    if ' - ' in full_title:
        artist, title = full_title.split(' - ', 1)
    elif ' — ' in full_title:
        artist, title = full_title.split(' — ', 1)
    else:
        artist, title = "", full_title
    # Tozalash
    for w in ['(Official Video)', '(Music Video)', 'HD', '4K', 'Lyrics', 'Cover']:
        title = title.replace(w, '')
        artist = artist.replace(w, '')
    title = re.sub(r'[\(\[].*?[\)\]]', '', title).strip()
    artist = re.sub(r'[\(\[].*?[\)\]]', '', artist).strip()
    return artist[:40], title[:60]

# =================== YT-DLP SOZLAMALARI (COOKIES BILAN) ===================
def get_ytdlp_opts(extra_opts: dict = None) -> dict:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
        'socket_timeout': 30,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'extractor_args': {'youtube': {'skip': ['dash', 'hls']}},
    }
    if COOKIES_AVAILABLE:
        opts['cookiefile'] = COOKIES_FILE
    if extra_opts:
        opts.update(extra_opts)
    return opts

# =================== YUKLASH FUNKSIYALARI ===================
async def download_video(url: str, user_id: int):
    def run():
        try:
            opts = get_ytdlp_opts({
                'outtmpl': str(Config.DOWNLOADS_PATH / f"video_{user_id}_{int(time.time())}.%(ext)s"),
                'format': 'best[height<=480][ext=mp4]/best[ext=mp4]',
                'merge_output_format': 'mp4',
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    base = filename.rsplit('.', 1)[0]
                    for ext in ['.mp4', '.webm', '.mkv']:
                        if os.path.exists(base + ext):
                            filename = base + ext
                            break
                return filename, info.get('title', 'Video'), info.get('duration', 0)
        except Exception as e:
            err = str(e)
            if "Sign in to confirm" in err:
                err = "❌ YouTube botni aniqladi! Iltimos, administrator cookies.txt qo'shsin."
            return None, err, 0
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def download_mp3(url: str, user_id: int):
    def run():
        try:
            opts = get_ytdlp_opts({
                'outtmpl': str(Config.DOWNLOADS_PATH / f"audio_{user_id}_{int(time.time())}.%(ext)s"),
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '128',
                }],
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                return filename, info.get('title', 'Audio')
        except Exception as e:
            err = str(e)
            if "Sign in to confirm" in err:
                err = "❌ YouTube botni aniqladi! Iltimos, administrator cookies.txt qo'shsin."
            return None, err
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def search_songs(query: str, limit: int = 8) -> List[dict]:
    def run():
        try:
            opts = get_ytdlp_opts({'quiet': True, 'extract_flat': True})
            search_query = f"ytsearch{limit}:{query}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                songs = []
                if 'entries' in info:
                    for i, entry in enumerate(info['entries'], 1):
                        if not entry:
                            continue
                        full_title = entry.get('title', 'Nomaʼlum')
                        artist, title = extract_artist_title(full_title)
                        songs.append({
                            'number': i,
                            'title': title[:60],
                            'artist': artist[:40],
                            'full_title': full_title[:80],
                            'duration': format_duration(entry.get('duration', 0)),
                            'url': f"https://youtube.com/watch?v={entry.get('id', '')}",
                        })
                return songs
        except Exception as e:
            logging.error(f"Qidiruv xatosi: {e}")
            return []
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== BOT BUYRUQLARI ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>MP3 yuklab oluvchi bot</b>\n\n"
        "🔍 <b>Qo'shiq nomi yozing</b> – qidirish va MP3 yuklash\n"
        "📥 <b>YouTube/Instagram/TikTok linki yuboring</b> – video ichidan MP3\n\n"
        "✍️ <b>Misol:</b> <code>Shoxruxon</code> yoki <code>Yalla</code>\n"
        "Bot hech qachon uxlamaydi va turli qo'shiqlarni chiqaradi."
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 Yordam:\n"
        "/start – Botni qayta ishga tushirish\n"
        "/help – Bu yordam\n\n"
        "🎧 Qo'shiq nomini yuborsangiz, 8 tagacha natija chiqadi.\n"
        "Har bir natija tugmasi bosilganda MP3 yuklanadi.\n\n"
        "⚠️ Agar YouTube xatolik bersa, bot egasiga <code>cookies.txt</code> kerakligini ayting."
    )

@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    if re.match(r'^https?://', text):
        await process_url(message, text, user_id)
    else:
        await process_search(message, text, user_id)

async def process_search(message: Message, query: str, user_id: int):
    status = await message.answer(f"🔍 <b>{query}</b> – qidirilmoqda...")
    songs = await search_songs(query, limit=8)
    await status.delete()
    if not songs:
        await message.answer("❌ Hech narsa topilmadi. Boshqa soʻz bilan urunib koʻring.")
        return

    builder = InlineKeyboardBuilder()
    for s in songs:
        song_id = hashlib.md5(s['url'].encode()).hexdigest()[:10]
        temp_data[song_id] = s
        btn_text = f"{s['number']}. {s['artist']} – {s['title']}" if s['artist'] else f"{s['number']}. {s['title']}"
        builder.button(text=btn_text[:60], callback_data=f"dl_{song_id}")
    builder.adjust(1)

    await message.answer(
        f"🎵 <b>Natijalar: {query}</b>\n\n👇 Yuklamoqchi boʻlgan qoʻshiqni tanlang:",
        reply_markup=builder.as_markup()
    )

async def process_url(message: Message, url: str, user_id: int):
    platform = get_platform(url)
    if platform == 'other':
        await message.answer("❌ Faqat YouTube, Instagram, TikTok, Facebook linklari ishlaydi.")
        return
    status = await message.answer("⏳ Video yuklanmoqda (1-2 daqiqa)...")
    filename, title, duration = await download_video(url, user_id)
    await status.delete()
    if not filename or not os.path.exists(filename):
        await message.answer(f"❌ Yuklab boʻlmadi:\n{title[:200]}")
        return
    file_size = os.path.getsize(filename)
    if file_size > Config.MAX_FILE_SIZE:
        await message.answer(f"❌ Video juda katta ({format_size(file_size)})")
        os.remove(filename)
        return
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    video_cache[url_hash] = {
        'url': url,
        'title': title,
        'duration': duration,
        'platform': platform
    }
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")]
    ])
    try:
        await message.answer_video(FSInputFile(filename),
                                   caption=f"📹 {title[:50]}\n⏱️ {format_duration(duration)}",
                                   reply_markup=keyboard)
    except Exception as e:
        await message.answer(f"❌ Video yuborish xatosi: {str(e)[:100]}")
    os.remove(filename)

# =================== MP3 YUKLASH CALLBACKLARI ===================
@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    song_id = call.data.replace("dl_", "")
    song = temp_data.get(song_id)
    if not song:
        await call.answer("❌ Maʼlumot eskirgan, qaytadan qidiring.", show_alert=True)
        return
    await call.answer("⏳ MP3 tayyorlanmoqda...")
    msg = await call.message.answer(f"⏳ <b>{song['full_title'][:50]}</b> yuklanmoqda...")
    filename, title = await download_mp3(song['url'], call.from_user.id)
    await msg.delete()
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @{call.bot.username}",
            title=title[:64],
            performer=song['artist'][:64] or "MP3 Bot"
        )
        os.remove(filename)
        temp_data.pop(song_id, None)
    else:
        await call.message.answer(f"❌ MP3 chiqarmadi:\n{title[:200]}")

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    url_hash = call.data.replace("mp3_", "")
    info = video_cache.get(url_hash)
    if not info:
        await call.answer("❌ Video maʼlumoti yoʻq, qaytadan link yuboring.", show_alert=True)
        return
    await call.answer("⏳ MP3 tayyor...")
    msg = await call.message.answer("⏳ MP3 ga aylantirilmoqda...")
    filename, title = await download_mp3(info['url'], call.from_user.id)
    await msg.delete()
    if filename and os.path.exists(filename):
        await call.message.answer_audio(FSInputFile(filename), title=title[:64])
        os.remove(filename)
    else:
        await call.message.answer(f"❌ MP3 yuklab boʻlmadi:\n{title[:200]}")

# =================== UXLAB QOLMASLIK MEXANIZMI ===================
async def keep_alive_server():
    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nalive")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(handler, '0.0.0.0', Config.KEEP_ALIVE_PORT)
    print(f"🟢 Keep-alive server: 0.0.0.0:{Config.KEEP_ALIVE_PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    async with aiohttp.ClientSession() as sess:
        while bot_running:
            try:
                async with sess.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        print(f"✅ Self-ping {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(Config.PING_INTERVAL)

# =================== ISHGA TUSHIRISH ===================
async def on_startup():
    print("🚀 Bot ishga tushmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook tozalandi")
    if COOKIES_AVAILABLE:
        print("🍪 cookies.txt mavjud, YouTube ishlaydi")
    else:
        print("⚠️ cookies.txt YO'Q, YouTube sign in xatosi chiqishi mumkin")

async def main():
    logging.basicConfig(level=logging.INFO)
    await on_startup()
    bot_info = await bot.get_me()
    print(f"🤖 @{bot_info.username} ishga tushdi. Keep-alive port: {Config.KEEP_ALIVE_PORT}")
    asyncio.create_task(keep_alive_server())
    asyncio.create_task(self_ping())
    await dp.start_polling(bot)

def signal_handler(sig, frame):
    global bot_running
    print("\n⏹️ Toʻxtatilmoqda...")
    bot_running = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("⏹️ Toʻxtatildi.")
