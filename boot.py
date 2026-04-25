import asyncio
import os
import re
import hashlib
import time
import subprocess
import signal
import sys
import json
import pickle
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
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

# =================== SHAZAM ===================
try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False

# =================== KONFIGURATSIYA ===================
load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DOWNLOADS_PATH = Path("/tmp/downloads")
    TEMP_PATH = Path("/tmp/temp_audio")
    CACHE_PATH = Path("/tmp/cache")  # Ma'lumotlarni saqlash uchun
    MAX_FILE_SIZE = 50 * 1024 * 1024
    AUDIO_SAMPLE_DURATION = 15
    KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))
    PING_INTERVAL = 300
    CACHE_TTL = 86400  # 24 soat (ma'lumotlar 1 kun saqlanadi)

if not Config.BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

# Papkalar
Config.DOWNLOADS_PATH.mkdir(exist_ok=True)
Config.TEMP_PATH.mkdir(exist_ok=True)
Config.CACHE_PATH.mkdir(exist_ok=True)

# =================== FFMPEG ===================
def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except:
        return False

FFMPEG_AVAILABLE = check_ffmpeg()

# =================== COOKIES ===================
COOKIES_FILE = "cookies.txt"
COOKIES_AVAILABLE = os.path.exists(COOKIES_FILE)
if COOKIES_AVAILABLE:
    print("✅ YouTube cookies topildi!")
else:
    print("⚠️ cookies.txt topilmadi!")

# =================== CACHE (MA'LUMOTLARNI SAQLASH) ===================
class DataCache:
    """Ma'lumotlarni saqlash va vaqt o'tganda ham ishlatish uchun"""
    
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.video_cache_file = cache_dir / "video_cache.json"
        self.song_cache_file = cache_dir / "song_cache.json"
        self.load()
    
    def load(self):
        """Saqlangan ma'lumotlarni yuklash"""
        self.video_cache = {}
        self.song_cache = {}
        
        if self.video_cache_file.exists():
            try:
                with open(self.video_cache_file, 'r') as f:
                    data = json.load(f)
                    # Eski ma'lumotlarni tozalash (24 soatdan eskilari)
                    now = time.time()
                    for key, value in data.items():
                        if now - value.get('timestamp', 0) < Config.CACHE_TTL:
                            self.video_cache[key] = value
                print(f"✅ {len(self.video_cache)} ta video ma'lumoti yuklandi")
            except:
                pass
        
        if self.song_cache_file.exists():
            try:
                with open(self.song_cache_file, 'r') as f:
                    data = json.load(f)
                    now = time.time()
                    for key, value in data.items():
                        if now - value.get('timestamp', 0) < Config.CACHE_TTL:
                            self.song_cache[key] = value
                print(f"✅ {len(self.song_cache)} ta song ma'lumoti yuklandi")
            except:
                pass
    
    def save(self):
        """Ma'lumotlarni saqlash"""
        with open(self.video_cache_file, 'w') as f:
            json.dump(self.video_cache, f, indent=2)
        with open(self.song_cache_file, 'w') as f:
            json.dump(self.song_cache, f, indent=2)
    
    def add_video(self, key: str, data: dict):
        """Video ma'lumotini saqlash"""
        data['timestamp'] = time.time()
        self.video_cache[key] = data
        self.save()
    
    def get_video(self, key: str) -> Optional[dict]:
        """Video ma'lumotini olish"""
        data = self.video_cache.get(key)
        if data:
            # Tekshirish: 24 soatdan eski bo'lsa o'chirish
            if time.time() - data.get('timestamp', 0) > Config.CACHE_TTL:
                del self.video_cache[key]
                self.save()
                return None
        return data
    
    def add_song(self, key: str, data: dict):
        """Song ma'lumotini saqlash"""
        data['timestamp'] = time.time()
        self.song_cache[key] = data
        self.save()
    
    def get_song(self, key: str) -> Optional[dict]:
        """Song ma'lumotini olish"""
        data = self.song_cache.get(key)
        if data:
            if time.time() - data.get('timestamp', 0) > Config.CACHE_TTL:
                del self.song_cache[key]
                self.save()
                return None
        return data

# Global cache
cache = DataCache(Config.CACHE_PATH)

# =================== BOT ===================
session = AiohttpSession(timeout=60)
bot = Bot(
    token=Config.BOT_TOKEN, 
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=2)

temp_data: Dict[str, dict] = {}
bot_running = True
shazam = Shazam() if SHAZAM_AVAILABLE else None

# =================== YORDAMCHI ===================
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
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}:{secs:02d}"

def format_size(bytes_size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

def get_ytdlp_opts(extra=None):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
        'socket_timeout': 30,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    if COOKIES_AVAILABLE:
        opts['cookiefile'] = COOKIES_FILE
    if extra:
        opts.update(extra)
    return opts

# =================== SHAZAM ===================
async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    if not SHAZAM_AVAILABLE or not shazam or not FFMPEG_AVAILABLE:
        return None
    
    try:
        audio_path = str(Config.TEMP_PATH / f"sample_{int(time.time())}.mp3")
        
        cmd = [
            'ffmpeg', '-i', video_path,
            '-ss', '5',
            '-t', str(Config.AUDIO_SAMPLE_DURATION),
            '-q:a', '0',
            '-map', 'a',
            audio_path,
            '-y'
        ]
        
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if not os.path.exists(audio_path):
            return None
        
        shazam_result = await shazam.recognize(audio_path)
        
        if os.path.exists(audio_path):
            os.remove(audio_path)
        
        if shazam_result and 'track' in shazam_result:
            track = shazam_result['track']
            return {
                'title': track.get('title', ''),
                'artist': track.get('subtitle', ''),
                'full_title': f"{track.get('subtitle', '')} - {track.get('title', '')}",
            }
        return None
    except Exception as e:
        logging.error(f"Audio aniqlashda xatolik: {e}")
        return None

# =================== YUKLASH ===================
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
            return None, str(e), 0
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
            error_msg = str(e)
            if "Sign in to confirm" in error_msg:
                error_msg = "❌ cookies.txt kerak!"
            return None, error_msg
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def search_songs(query: str, limit: int = 10) -> List[dict]:
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
                            'number': i,
                            'artist': artist,
                            'title': title,
                            'duration': format_duration(item.get('duration', 0)),
                            'url': f"https://youtube.com/watch?v={item.get('id', '')}",
                        })
                return songs
        except Exception as e:
            logging.error(f"Qidiruv xatosi: {e}")
            return []
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== BOT ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>MP3 Bot</b> 🎵\n\n"
        "📥 <b>Link yuboring:</b>\n"
        "YouTube | Instagram | TikTok | Facebook\n\n"
        "🔍 <b>Qo'shiq qidirish:</b>\n"
        "Masalan: Shohruhxon\n\n"
        "✅ <b>Tugmalar vaqt o'tganda ham ishlaydi!</b>\n"
        "⚡ 7/24 ishlaydi"
    )

@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    
    if re.match(r'^https?://', text):
        await process_url(message, text, user_id)
    else:
        await process_search(message, text, user_id)

async def process_url(message: Message, url: str, user_id: int):
    platform = get_platform(url)
    
    if platform == 'other':
        await message.answer("❌ Faqat YouTube, Instagram, TikTok, Facebook linklari!")
        return
    
    if platform != 'youtube':
        await handle_social_video(message, url, user_id, platform)
    else:
        await handle_youtube(message, url, user_id)

# YouTube: faqat MP3 tugmasi (tezroq)
async def handle_youtube(message: Message, url: str, user_id: int):
    msg = await message.answer("⏳ Ma'lumot olinmoqda...")
    
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    
    # Avval keshdan tekshirish
    cached = cache.get_video(url_hash)
    if cached:
        title = cached.get('title', 'YouTube Video')
        duration = cached.get('duration', 0)
    else:
        def get_info():
            try:
                opts = get_ytdlp_opts({'extract_flat': True})
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return info.get('title', 'Video'), info.get('duration', 0)
            except:
                return url[:50], 0
        
        title, duration = await asyncio.get_event_loop().run_in_executor(pool, get_info)
        cache.add_video(url_hash, {'url': url, 'title': title, 'duration': duration, 'platform': platform})
    
    await msg.delete()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
        [InlineKeyboardButton(text="🔍 O'xshash qo'shiqlar", callback_data=f"similar_{url_hash}")]
    ])
    
    await message.answer(
        f"🎬 <b>YouTube</b>\n📹 <b>{title[:60]}</b>\n⏱️ {format_duration(duration)}\n\n👇 Tanlang:",
        reply_markup=keyboard
    )

# Instagram/TikTok/Facebook: video + tugmalar
async def handle_social_video(message: Message, url: str, user_id: int, platform: str):
    status = await message.answer("⏳ Video yuklanmoqda... (1-2 daqiqa)")
    
    filename, title, duration = await download_video(url, user_id)
    await status.delete()
    
    if not filename or not os.path.exists(filename):
        await message.answer("❌ Yuklab bo'lmadi!")
        return
    
    file_size = os.path.getsize(filename)
    if file_size > Config.MAX_FILE_SIZE:
        await message.answer(f"❌ Video juda katta ({format_size(file_size)})")
        os.remove(filename)
        return
    
    # Shazam aniqlash
    identified_song = None
    if SHAZAM_AVAILABLE and FFMPEG_AVAILABLE:
        detect_msg = await message.answer("🎵 Video ichidagi qo'shiq aniqlanmoqda (Shazam)...")
        identified_song = await identify_audio_from_video(filename)
        await detect_msg.delete()
    
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    
    if identified_song:
        search_title = identified_song['full_title']
        detected_text = f"\n\n🎯 <b>Shazam topdi:</b> {identified_song['full_title'][:60]}"
    else:
        search_title = title
        detected_text = ""
    
    # Keshga saqlash
    cache.add_video(url_hash, {
        'url': url, 
        'title': title, 
        'duration': duration,
        'platform': platform,
        'identified_song': identified_song,
        'search_query': search_title
    })
    
    platform_emoji = {'instagram': '📸', 'tiktok': '🎵', 'facebook': '📘'}
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
        [InlineKeyboardButton(text="🔍 O'xshash qo'shiqlar", callback_data=f"similar_{url_hash}")]
    ])
    
    caption = f"{platform_emoji.get(platform, '📹')} <b>{title[:60]}</b>\n⏱️ {format_duration(duration)}\n📦 {format_size(file_size)}"
    if detected_text:
        caption += detected_text
    
    await message.answer_video(
        FSInputFile(filename),
        caption=caption,
        reply_markup=keyboard
    )
    os.remove(filename)

# Qidiruv
async def process_search(message: Message, query: str, user_id: int):
    status = await message.answer(f"🔍 <b>{query}</b> qidirilmoqda...")
    songs = await search_songs(query, limit=10)
    await status.delete()
    
    if not songs:
        await message.answer("❌ Hech narsa topilmadi!")
        return
    
    result = f"🎵 <b>{query}</b>\n\n"
    for s in songs:
        if s['artist']:
            result += f"<b>{s['number']}.</b> {s['artist']} - {s['title']}  <code>{s['duration']}</code>\n"
        else:
            result += f"<b>{s['number']}.</b> {s['title']}  <code>{s['duration']}</code>\n"
    
    builder = InlineKeyboardBuilder()
    for s in songs:
        song_id = hashlib.md5(s['url'].encode()).hexdigest()[:10]
        temp_data[song_id] = s
        # Qidiruv tugmalari 24 soat saqlanadi
        cache.add_song(song_id, s)
        builder.button(text=f"{s['number']}. {s['title'][:30]}", callback_data=f"dl_{song_id}")
    builder.adjust(2)
    
    await message.answer(result, reply_markup=builder.as_markup())

# MP3 yuklash
@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    song_id = call.data.replace("dl_", "")
    
    # Avval temp_data dan, keyin cachedan tekshirish
    song = temp_data.get(song_id)
    if not song:
        song = cache.get_song(song_id)
    
    if not song:
        await call.answer("❌ Ma'lumot eskirgan! Qaytadan qidiring.", show_alert=True)
        return
    
    await call.answer("⏳")
    msg = await call.message.answer(f"⏳ {song.get('title', 'Qo\'shiq')[:40]} yuklanmoqda...")
    
    filename, result = await download_mp3(song['url'], call.from_user.id)
    await msg.delete()
    
    if filename and os.path.exists(filename):
        size = os.path.getsize(filename)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{song.get('title', 'Qo\'shiq')[:50]}</b>\n📦 {format_size(size)}",
            title=song.get('title', 'Audio')[:64]
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ {result[:200]}")

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_url(call: CallbackQuery):
    url_hash = call.data.replace("mp3_", "")
    
    # Keshdan video ma'lumotini olish
    song = cache.get_video(url_hash)
    if not song:
        await call.answer("❌ Ma'lumot topilmadi! Qaytadan link yuboring.", show_alert=True)
        return
    
    await call.answer("⏳")
    msg = await call.message.answer("⏳ MP3 yuklanmoqda...")
    
    filename, result = await download_mp3(song['url'], call.from_user.id)
    await msg.delete()
    
    if filename and os.path.exists(filename):
        size = os.path.getsize(filename)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{song.get('title', 'Qo\'shiq')[:50]}</b>\n📦 {format_size(size)}",
            title=song.get('title', 'Audio')[:64]
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ {result[:200]}")

# O'xshash qo'shiqlar
@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    url_hash = call.data.replace("similar_", "")
    
    song = cache.get_video(url_hash)
    if not song:
        await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        return
    
    await call.answer("🔍")
    
    if song.get('identified_song'):
        search_query = song['identified_song']['full_title']
    else:
        search_query = song.get('title', '')
        if ' - ' in search_query:
            search_query = search_query.split(' - ')[0]
    
    msg = await call.message.answer(f"🔍 {search_query[:40]} o'xshashlari...")
    
    songs = await search_songs(search_query, limit=10)
    await msg.delete()
    
    if not songs:
        await call.message.answer("❌ O'xshash qo'shiqlar topilmadi!")
        return
    
    result = f"🎵 <b>O'xshash qo'shiqlar:</b>\n📌 {search_query[:50]}\n\n"
    for s in songs:
        if s['artist']:
            result += f"<b>{s['number']}.</b> {s['artist']} - {s['title']}  <code>{s['duration']}</code>\n"
        else:
            result += f"<b>{s['number']}.</b> {s['title']}  <code>{s['duration']}</code>\n"
    
    builder = InlineKeyboardBuilder()
    for s in songs:
        song_id = hashlib.md5(s['url'].encode()).hexdigest()[:10]
        temp_data[song_id] = s
        cache.add_song(song_id, s)
        builder.button(text=f"{s['number']}. {s['title'][:30]}", callback_data=f"dl_{song_id}")
    builder.adjust(2)
    builder.button(text="◀️ Ortga", callback_data=f"back_{url_hash}")
    
    await call.message.answer(result, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("back_"))
async def go_back(call: CallbackQuery):
    url_hash = call.data.replace("back_", "")
    song = cache.get_video(url_hash)
    
    if not song:
        await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
        [InlineKeyboardButton(text="🔍 O'xshash qo'shiqlar", callback_data=f"similar_{url_hash}")]
    ])
    
    await call.message.edit_text(
        f"📹 <b>{song.get('title', 'Video')[:50]}</b>\n⏱️ {format_duration(song.get('duration', 0))}\n\n👇 Tanlang:",
        reply_markup=keyboard
    )

# =================== KEEP-ALIVE ===================
async def keep_alive():
    async def handler(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\n\r\nalive")
        await writer.drain()
        writer.close()
    
    server = await asyncio.start_server(handler, '0.0.0.0', Config.KEEP_ALIVE_PORT)
    print(f"🟢 Keep-alive: 0.0.0.0:{Config.KEEP_ALIVE_PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    async with aiohttp.ClientSession() as sess:
        while bot_running:
            try:
                await sess.get(f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}", timeout=5)
            except:
                pass
            await asyncio.sleep(Config.PING_INTERVAL)

# =================== STARTUP ===================
@dp.startup()
async def on_startup():
    print("🚀 Bot ishga tushmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook tozalandi")
    print(f"✅ Cache: {Config.CACHE_PATH}")
    print(f"✅ Ma'lumotlar 24 soat saqlanadi")
    if COOKIES_AVAILABLE:
        print("✅ YouTube cookies mavjud")
    else:
        print("⚠️ cookies.txt yo'q")

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
