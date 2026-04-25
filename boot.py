import asyncio
import os
import re
import hashlib
import time
import subprocess
import signal
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
import logging
import aiohttp

load_dotenv()

# ==================== KONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))
DOWNLOADS_PATH = Path("/tmp/downloads")
DOWNLOADS_PATH.mkdir(exist_ok=True)

COOKIES_FILE = "cookies.txt"
COOKIES_AVAILABLE = os.path.exists(COOKIES_FILE)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

# ==================== BOT ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=3)
bot_running = True

# Kesh
search_cache = {}
song_cache = {}

# ==================== YORDAMCHI ====================
def get_platform(url: str) -> str:
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'instagram.com' in url_lower or 'instagr.am' in url_lower:
        return 'instagram'
    elif 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'facebook.com' in url_lower or 'fb.watch' in url_lower:
        return 'facebook'
    return 'other'

def format_duration(seconds):
    if not seconds:
        return "0:00"
    return f"{int(seconds//60)}:{int(seconds%60):02d}"

def format_size(size):
    for u in ['B', 'KB', 'MB']:
        if size < 1024:
            return f"{size:.1f} {u}"
        size /= 1024
    return f"{size:.1f} GB"

def get_ytdlp_opts(extra=None):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'retries': 2,
        'socket_timeout': 20,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    if COOKIES_AVAILABLE:
        opts['cookiefile'] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts

# ==================== QIDIRUV ====================
async def search_songs(query: str, limit: int = 10):
    cache_key = f"{query}_{limit}"
    if cache_key in search_cache:
        return search_cache[cache_key]
    
    def run():
        try:
            opts = get_ytdlp_opts({'extract_flat': True, 'playlistend': limit})
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                songs = []
                for i, item in enumerate(data.get('entries', []), 1):
                    if item:
                        title = item.get('title', 'Noma\'lum')
                        artist = ""
                        if ' - ' in title:
                            parts = title.split(' - ', 1)
                            artist = parts[0][:35]
                            title = parts[1][:55]
                        songs.append({
                            'num': i,
                            'artist': artist,
                            'title': title,
                            'duration': format_duration(item.get('duration', 0)),
                            'url': f"https://youtube.com/watch?v={item.get('id', '')}",
                        })
                return songs
        except Exception as e:
            logging.error(f"Qidiruv xatosi: {e}")
            return []
    
    songs = await asyncio.get_event_loop().run_in_executor(pool, run)
    if songs:
        search_cache[cache_key] = songs
    return songs

# ==================== MP3 YUKLASH (YouTube uchun) ====================
async def download_mp3(url: str, user_id: int):
    def run():
        try:
            opts = get_ytdlp_opts({
                'outtmpl': str(DOWNLOADS_PATH / f"{user_id}_{int(time.time())}.%(ext)s"),
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '128',
                }],
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.mp3'
                return filename, info.get('title', 'Audio')
        except Exception as e:
            return None, str(e)
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# ==================== VIDEO YUKLASH (Instagram/TikTok/Facebook uchun) ====================
async def download_video(url: str, user_id: int):
    def run():
        try:
            opts = get_ytdlp_opts({
                'outtmpl': str(DOWNLOADS_PATH / f"{user_id}_video_{int(time.time())}.%(ext)s"),
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
            return None, str(e), 0
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# ==================== BOT BUYRUQLARI ====================
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎵 <b>MP3 Bot</b>\n\n"
        "🔍 <b>Qo'shiq nomi yozing</b> - qidirish va MP3 yuklash\n"
        "📥 <b>YouTube linki</b> - MP3 yuklash\n"
        "📸 <b>Instagram/TikTok/Facebook linki</b> - video + MP3\n\n"
        "⚡ Tez, bepul, 7/24 ishlaydi"
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "📖 <b>Yordam</b>\n\n"
        "1. Qo'shiq nomi yozing - ro'yxat chiqadi\n"
        "2. Raqamni bosing - MP3 yuklanadi\n"
        "3. YouTube linki - MP3 tugmasi\n"
        "4. Instagram/TikTok linki - video + MP3 tugmasi\n\n"
        "🔍 O'xshash qo'shiqlar tugmasi ham bor"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text.strip()
    
    if re.match(r'^https?://', text):
        await handle_url(message, text)
    else:
        await handle_search(message, text)

# ==================== QIDIRUV ====================
async def handle_search(message: Message, query: str):
    msg = await message.answer(f"🔍 <b>{query}</b> qidirilmoqda...")
    songs = await search_songs(query, limit=10)
    await msg.delete()
    
    if not songs:
        await message.answer("❌ Hech narsa topilmadi!")
        return
    
    result = f"🎵 <b>{query}</b>\n\n"
    for s in songs:
        if s['artist']:
            result += f"<b>{s['num']}.</b> {s['artist']} - {s['title']}  <code>{s['duration']}</code>\n"
        else:
            result += f"<b>{s['num']}.</b> {s['title']}  <code>{s['duration']}</code>\n"
    
    builder = InlineKeyboardBuilder()
    for s in songs:
        song_id = hashlib.md5(s['url'].encode()).hexdigest()[:10]
        song_cache[song_id] = s
        builder.button(text=f"{s['num']}. {s['title'][:30]}", callback_data=f"dl_{song_id}")
    builder.adjust(2)
    
    await message.answer(result, reply_markup=builder.as_markup())

# ==================== URL (YouTube = MP3, Boshqalar = Video) ====================
async def handle_url(message: Message, url: str):
    platform = get_platform(url)
    
    if platform == 'other':
        await message.answer("❌ Faqat YouTube, Instagram, TikTok, Facebook linklari!")
        return
    
    # YouTube - faqat MP3 tugmasi (video yubormaydi)
    if platform == 'youtube':
        await handle_youtube(message, url)
    else:
        # Instagram, TikTok, Facebook - video + tugmalar
        await handle_social_video(message, url, platform)

# YouTube: faqat MP3 tugmasi
async def handle_youtube(message: Message, url: str):
    msg = await message.answer("⏳ Ma'lumot olinmoqda...")
    
    def get_info():
        try:
            opts = get_ytdlp_opts({'extract_flat': True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get('title', 'Video'), info.get('duration', 0)
        except:
            return url[:50], 0
    
    title, duration = await asyncio.get_event_loop().run_in_executor(pool, get_info)
    await msg.delete()
    
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    song_cache[url_hash] = {'url': url, 'title': title, 'duration': duration}
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
        [InlineKeyboardButton(text="🔍 O'xshash qo'shiqlar", callback_data=f"similar_{url_hash}")]
    ])
    
    await message.answer(
        f"🎬 <b>YouTube</b>\n📹 <b>{title[:60]}</b>\n⏱️ {format_duration(duration)}\n\n👇 Tanlang:",
        reply_markup=keyboard
    )

# Instagram/TikTok/Facebook: video + tugmalar
async def handle_social_video(message: Message, url: str, platform: str):
    msg = await message.answer("⏳ Video yuklanmoqda... (1-2 daqiqa)")
    
    filename, title, duration = await download_video(url, message.from_user.id)
    await msg.delete()
    
    if not filename or not os.path.exists(filename):
        await message.answer(f"❌ Yuklab bo'lmadi!")
        return
    
    file_size = os.path.getsize(filename)
    if file_size > 50 * 1024 * 1024:
        await message.answer(f"❌ Video juda katta ({format_size(file_size)})")
        os.remove(filename)
        return
    
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    song_cache[url_hash] = {'url': url, 'title': title, 'duration': duration}
    
    platform_emoji = {
        'instagram': '📸',
        'tiktok': '🎵',
        'facebook': '📘'
    }
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
        [InlineKeyboardButton(text="🔍 O'xshash qo'shiqlar", callback_data=f"similar_{url_hash}")]
    ])
    
    await message.answer_video(
        FSInputFile(filename),
        caption=f"{platform_emoji.get(platform, '📹')} <b>{title[:60]}</b>\n⏱️ {format_duration(duration)}\n📦 {format_size(file_size)}",
        reply_markup=keyboard
    )
    os.remove(filename)

# ==================== MP3 YUKLASH TUGMASI ====================
@dp.callback_query(F.data.startswith("dl_"))
async def download_song(call: CallbackQuery):
    song_id = call.data.replace("dl_", "")
    song = song_cache.get(song_id)
    
    if not song:
        await call.answer("❌ Qaytadan qidiring!", show_alert=True)
        return
    
    await call.answer("⏳ Yuklanmoqda...")
    msg = await call.message.answer(f"⏳ <b>{song['title'][:40]}</b> yuklanmoqda...")
    
    filename, result = await download_mp3(song['url'], call.from_user.id)
    await msg.delete()
    
    if filename and os.path.exists(filename):
        size = os.path.getsize(filename)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{song['title'][:50]}</b>\n📦 {format_size(size)}",
            title=song['title'][:64]
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ {result[:150]}")

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_url(call: CallbackQuery):
    url_hash = call.data.replace("mp3_", "")
    song = song_cache.get(url_hash)
    
    if not song:
        await call.answer("❌ Qaytadan link yuboring!", show_alert=True)
        return
    
    await call.answer("⏳")
    msg = await call.message.answer("⏳ MP3 yuklanmoqda...")
    
    filename, result = await download_mp3(song['url'], call.from_user.id)
    await msg.delete()
    
    if filename and os.path.exists(filename):
        size = os.path.getsize(filename)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{song['title'][:50]}</b>\n📦 {format_size(size)}",
            title=song['title'][:64]
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ {result[:150]}")

# ==================== OXSHASH QO'SHIQLAR ====================
@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    url_hash = call.data.replace("similar_", "")
    song = song_cache.get(url_hash)
    
    if not song:
        await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        return
    
    await call.answer("🔍 Qidirilmoqda...")
    
    search_query = song['title']
    if ' - ' in search_query:
        search_query = search_query.split(' - ')[0]
    
    msg = await call.message.answer(f"🔍 <b>{search_query[:40]}</b> o'xshashlari...")
    
    songs = await search_songs(search_query, limit=8)
    await msg.delete()
    
    if not songs:
        await call.message.answer("❌ O'xshash qo'shiqlar topilmadi!")
        return
    
    result = f"🎵 <b>O'xshash qo'shiqlar:</b>\n📌 {search_query[:50]}\n\n"
    for s in songs:
        if s['artist']:
            result += f"<b>{s['num']}.</b> {s['artist']} - {s['title']}  <code>{s['duration']}</code>\n"
        else:
            result += f"<b>{s['num']}.</b> {s['title']}  <code>{s['duration']}</code>\n"
    
    builder = InlineKeyboardBuilder()
    for s in songs:
        song_id = hashlib.md5(s['url'].encode()).hexdigest()[:10]
        song_cache[song_id] = s
        builder.button(text=f"{s['num']}. {s['title'][:30]}", callback_data=f"dl_{song_id}")
    builder.adjust(2)
    builder.button(text="◀️ Ortga", callback_data=f"back_{url_hash}")
    
    await call.message.answer(result, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("back_"))
async def go_back(call: CallbackQuery):
    url_hash = call.data.replace("back_", "")
    song = song_cache.get(url_hash)
    
    if not song:
        await call.answer("❌", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
        [InlineKeyboardButton(text="🔍 O'xshash qo'shiqlar", callback_data=f"similar_{url_hash}")]
    ])
    
    await call.message.edit_text(
        f"📹 <b>{song['title'][:50]}</b>\n⏱️ {song.get('duration', '0:00')}\n\n👇 Tanlang:",
        reply_markup=keyboard
    )

# ==================== UXLAB QOLMASLIK ====================
async def keep_alive():
    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nalive")
        await writer.drain()
        writer.close()
    
    server = await asyncio.start_server(handler, '0.0.0.0', PORT)
    print(f"🟢 Keep-alive: 0.0.0.0:{PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    async with aiohttp.ClientSession() as sess:
        while bot_running:
            try:
                await sess.get(f"http://127.0.0.1:{PORT}", timeout=5)
            except:
                pass
            await asyncio.sleep(300)

# ==================== ISHGA TUSHIRISH ====================
@dp.startup()
async def on_startup():
    print("🚀 Bot ishga tushmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook tozalandi")
    if COOKIES_AVAILABLE:
        print("✅ Cookies mavjud (YouTube ishlaydi)")
    else:
        print("⚠️ Cookies yo'q")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    asyncio.create_task(keep_alive())
    asyncio.create_task(self_ping())
    
    bot_info = await bot.get_me()
    print(f"🤖 @{bot_info.username} ishga tushdi")
    
    await dp.start_polling(bot)

def signal_handler(sig, frame):
    global bot_running
    print("\n⏹️ To'xtatilmoqda...")
    bot_running = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("To'xtatildi.")
