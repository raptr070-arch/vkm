import asyncio
import os
import re
import hashlib
import json
import time
import subprocess
import signal
import sys
import shutil
import base64
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
import logging
import aiohttp

# ShazamIO
try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False

# =================== KONFIGURATSIYA ===================
load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DOWNLOADS_PATH = Path("downloads")
    TEMP_PATH = Path("temp_audio")
    MAX_FILE_SIZE = 50 * 1024 * 1024
    AUDIO_SAMPLE_DURATION = 15
    KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))
    PING_INTERVAL = 300

if not Config.BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

# =================== DATA MODELS ===================
@dataclass
class SongData:
    id: str
    url: str
    title: str
    duration: str = "0:00"
    artist: str = ""
    platform: str = 'youtube'

# =================== COOKIE QO'LLAB-QUVVATLASH ===================
COOKIES_PATH = Path("/app/cookies.txt")

def get_cookie_file():
    """Cookie faylni topish - 4 xil usul"""
    
    # 1. Local fayl (GitHub repo dagi)
    if Path("cookies.txt").exists():
        print("✅ Local cookies.txt topildi")
        return str(Path("cookies.txt"))
    
    # 2. /app/cookies.txt (Railway da)
    if COOKIES_PATH.exists():
        print("✅ /app/cookies.txt topildi")
        return str(COOKIES_PATH)
    
    # 3. Base64 dan (COOKIE_BASE64)
    cookie_b64 = os.getenv("COOKIE_BASE64")
    if cookie_b64:
        try:
            cookie_content = base64.b64decode(cookie_b64).decode('utf-8')
            cookie_path = Config.DOWNLOADS_PATH / "cookies.txt"
            cookie_path.write_text(cookie_content)
            print("✅ Cookie base64 dan yuklandi")
            return str(cookie_path)
        except Exception as e:
            print(f"⚠️ Base64 xato: {e}")
    
    # 4. To'g'ridan-to'g'ri matndan (COOKIE_CONTENT)
    cookie_env = os.getenv("COOKIE_CONTENT")
    if cookie_env:
        try:
            cookie_path = Config.DOWNLOADS_PATH / "cookies.txt"
            cookie_path.write_text(cookie_env)
            print("✅ Cookie env dan yuklandi")
            return str(cookie_path)
        except Exception as e:
            print(f"⚠️ Env xato: {e}")
    
    print("⚠️ Cookie topilmadi - YouTube ba'zi videolar ishlamasligi mumkin")
    return None

COOKIE_FILE = get_cookie_file()

def get_ydl_opts(extra=None):
    """yt-dlp sozlamalari - cookie bilan"""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'geo_bypass': True,
        'retries': 10,
        'sleep_interval': 5,
        'max_sleep_interval': 10,
        'extractor_retries': 5,
        'noplaylist': True,
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'skip': ['hls', 'dash'],
                'player_skip': ['webpage', 'configs'],
            }
        }
    }
    
    # Cookie qo'shish
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
        print(f"🍪 Cookie ishlatilmoqda: {COOKIE_FILE}")
    
    if extra:
        opts.update(extra)
    return opts

# =================== INITIALIZATION ===================
Config.DOWNLOADS_PATH.mkdir(exist_ok=True)
Config.TEMP_PATH.mkdir(exist_ok=True)

session = AiohttpSession(timeout=120)
bot = Bot(
    token=Config.BOT_TOKEN, 
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=2)

temp_data: Dict[str, SongData] = {}
video_cache: Dict[str, dict] = {}
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True

# =================== YORDAMCHI FUNKSIYALAR ===================
def get_platform(url: str) -> str:
    url_lower = url.lower()
    patterns = {
        'youtube': ['youtube.com', 'youtu.be'],
        'instagram': ['instagram.com', 'instagr.am'],
        'tiktok': ['tiktok.com', 'vm.tiktok.com'],
        'facebook': ['facebook.com', 'fb.watch', 'fb.com'],
    }
    for platform, domains in patterns.items():
        if any(domain in url_lower for domain in domains):
            return platform
    return 'other'

def format_duration(seconds):
    if not seconds:
        return "0:00"
    try:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"
    except:
        return "0:00"

def format_size(bytes_size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f}TB"

def extract_artist_title(full_title: str):
    if not full_title:
        return "", ""
    
    if ' - ' in full_title:
        parts = full_title.split(' - ', 1)
        artist = parts[0].strip()
        title = parts[1].strip()
    elif ' — ' in full_title:
        parts = full_title.split(' — ', 1)
        artist = parts[0].strip()
        title = parts[1].strip()
    else:
        artist = ""
        title = full_title.strip()
    
    clean_title = title
    clean_title = re.sub(r'\(.*?\)|\[.*?\]|Official.*|MV|Music Video|Lyrics|HD|4K|Cover|Remix', '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
    clean_artist = re.sub(r'\s+', ' ', clean_artist).strip()
    
    if not clean_title or len(clean_title) < 3:
        clean_title = title.strip()
    
    return clean_artist[:30], clean_title[:50]

# =================== AUDIO ANIQLASH ===================
async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    if not SHAZAM_AVAILABLE or not shazam:
        return None
    
    try:
        audio_path = video_path.replace('.mp4', '_sample.mp3').replace('.webm', '_sample.mp3')
        
        cmd = [
            'ffmpeg', '-i', video_path,
            '-ss', '5',
            '-t', str(Config.AUDIO_SAMPLE_DURATION),
            '-q:a', '0',
            '-map', 'a',
            audio_path,
            '-y'
        ]
        
        subprocess.run(cmd, capture_output=True, text=True)
        
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

# =================== VIDEO YUKLASH ===================
async def download_video(url: str, user_id: int):
    def run():
        for attempt in range(3):
            try:
                opts = get_ydl_opts({
                    'outtmpl': str(Config.DOWNLOADS_PATH / f"v_{user_id}_{int(time.time())}_{attempt}_%(title)s.%(ext)s"),
                    'format': 'best[height<=480][ext=mp4]/best[ext=mp4]'
                })
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                    
                    if not os.path.exists(filename):
                        base = filename.rsplit('.', 1)[0]
                        for ext in ['.mp4', '.webm', '.mkv']:
                            test_path = base + ext
                            if os.path.exists(test_path):
                                filename = test_path
                                break
                    
                    full_title = info.get('title', 'Video')
                    duration = info.get('duration', 0)
                    return filename, full_title, duration
            except Exception as e:
                err = str(e)
                if attempt == 2:
                    return None, err, 0
                time.sleep(3)
        return None, "Noma'lum xato", 0
    
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== MP3 YUKLASH ===================
async def download_mp3(url: str, user_id: int):
    def run():
        for attempt in range(3):
            try:
                opts = get_ydl_opts({
                    'outtmpl': str(Config.DOWNLOADS_PATH / f"a_{user_id}_{int(time.time())}_{attempt}_%(title)s.%(ext)s"),
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                    title = info.get('title', 'Audio')
                    return filename, title
            except Exception as e:
                err = str(e)
                if attempt == 2:
                    return None, err
                time.sleep(3)
        return None, "Noma'lum xato"
    
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== QO'SHIQ QIDIRISH ===================
async def search_songs(query: str, limit: int = 10) -> List[dict]:
    def run():
        try:
            opts = get_ydl_opts({'extract_flat': True})
            search_query = f"ytsearch{limit}:{query}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                songs = []
                if 'entries' in info:
                    for i, item in enumerate(info['entries'], 1):
                        if item:
                            full_title = item.get('title', 'Nomalum')
                            artist, title = extract_artist_title(full_title)
                            songs.append({
                                'number': i,
                                'title': title[:60],
                                'artist': artist[:40],
                                'full_title': full_title[:80],
                                'duration': format_duration(item.get('duration', 0)),
                                'url': f"https://youtube.com/watch?v={item.get('id', '')}",
                            })
                return songs
        except Exception as e:
            print(f"Qidiruv xatosi: {e}")
            return []
    
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== HANDLERS ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>Zurnavolar Bot</b>\n\n"
        "📥 Link yuboring\n"
        "🔍 Qo'shiq nomi yozing\n\n"
        "@zurnavolarbot"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Yordam</b>\n\n"
        "🎯 YouTube/Instagram/TikTok/Facebook linki\n"
        "🔍 Qo'shiq nomi yozing\n"
        "🎵 MP3: 192kbps\n\n"
        "@zurnavolarbot"
    )

@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    
    if re.match(r'^https?://', text):
        await process_url(message, text, user_id)
    elif len(text) >= 2:
        await process_search(message, text, user_id)
    else:
        await message.answer("❌ Kamida 2 harf yoki link")

async def process_url(message: Message, url: str, user_id: int):
    platform = get_platform(url)
    
    if platform == 'other':
        await message.answer("❌ Faqat YouTube, Instagram, TikTok, Facebook")
        return
    
    status = await message.answer("⏳ Yuklanmoqda...")
    
    try:
        filename, full_title, duration = await asyncio.wait_for(download_video(url, user_id), timeout=90)
    except asyncio.TimeoutError:
        await status.delete()
        await message.answer("❌ Vaqt tugadi")
        return
    
    await status.delete()
    
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        
        if file_size > Config.MAX_FILE_SIZE:
            await message.answer(f"❌ Juda katta: {format_size(file_size)}")
            os.remove(filename)
            return
        
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        artist, title = extract_artist_title(full_title)
        
        identified_song = None
        if platform in ['instagram', 'tiktok', 'facebook']:
            detect_msg = await message.answer("🎵 Aniqlanmoqda...")
            identified_song = await identify_audio_from_video(filename)
            await detect_msg.delete()
        
        if identified_song:
            search_title = identified_song['full_title']
            search_artist = identified_song['artist']
        else:
            search_title = full_title
            search_artist = artist
        
        video_cache[url_hash] = {
            'url': url,
            'title': full_title,
            'artist': search_artist,
            'clean_title': title,
            'duration': duration,
            'platform': platform,
            'identified_song': identified_song,
            'search_query': search_title,
        }
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3_{url_hash}")],
            [InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"sim_{url_hash}")]
        ])
        
        platform_emoji = {'youtube': '🎬', 'instagram': '📸', 'tiktok': '🎵', 'facebook': '📘'}
        video_file = FSInputFile(filename)
        
        caption = f"{platform_emoji.get(platform, '📹')} <b>{title[:45]}</b>  {format_duration(duration)}"
        if identified_song:
            caption += f"\n🎯 {identified_song['full_title'][:40]}"
        caption += f"\n\n❤️ @zurnavolarbot"
        
        await message.answer_video(video_file, caption=caption, reply_markup=keyboard)
        os.remove(filename)
    else:
        await message.answer(f"❌ {full_title[:100]}")

async def process_search(message: Message, query: str, user_id: int):
    status = await message.answer(f"🔍 {query}")
    songs = await search_songs(query, limit=10)
    await status.delete()
    
    if not songs:
        await message.answer("❌ Topilmadi")
        return
    
    result = f"🔍 {query}\n\n"
    for s in songs:
        if s['artist']:
            result += f"{s['number']}. {s['artist']} - {s['title']} {s['duration']}\n"
        else:
            result += f"{s['number']}. {s['title']} {s['duration']}\n"
    
    builder = InlineKeyboardBuilder()
    for song in songs:
        song_id = hashlib.md5(song['url'].encode()).hexdigest()[:8]
        temp_data[song_id] = SongData(
            id=song_id, url=song['url'], title=song['full_title'],
            duration=song['duration'], artist=song['artist'], platform='youtube'
        )
        builder.button(text=f"{song['number']}", callback_data=f"dl_{song_id}")
    
    builder.adjust(5)
    await message.answer(
        f"{result}\n👇 <b>Raqamni bosing</b>\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    url_hash = call.data.replace("mp3_", "")
    video_info = video_cache.get(url_hash)
    
    if not video_info:
        await call.answer("❌", show_alert=True)
        return
    
    await call.answer("⏳")
    
    display_title = video_info.get('identified_song', {}).get('full_title', video_info['title'])[:40]
    status = await call.message.answer(f"⏳ {display_title}...")
    
    try:
        filename, title = await asyncio.wait_for(download_mp3(video_info['url'], call.from_user.id), timeout=90)
    except asyncio.TimeoutError:
        await status.delete()
        await call.message.answer("❌ Vaqt tugadi")
        return
    
    await status.delete()
    
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 {title[:45]}\n📦 {format_size(file_size)}\n\n❤️ @zurnavolarbot",
            title=title[:60],
            performer="Zurnavolar"
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ {title[:100]}")

@dp.callback_query(F.data.startswith("sim_"))
async def similar_songs(call: CallbackQuery):
    url_hash = call.data.replace("sim_", "")
    video_info = video_cache.get(url_hash)
    
    if not video_info:
        await call.answer("❌", show_alert=True)
        return
    
    await call.answer("🔍")
    
    if video_info.get('identified_song'):
        search_query = video_info['identified_song']['full_title']
    else:
        search_query = f"{video_info.get('artist', '')} {video_info.get('clean_title', '')}".strip()
    
    status = await call.message.answer(f"🔍 {search_query[:40]}...")
    
    all_songs = []
    seen_urls = set()
    
    if search_query:
        songs1 = await search_songs(search_query, limit=10)
        for s in songs1:
            if s['url'] not in seen_urls:
                all_songs.append(s)
                seen_urls.add(s['url'])
    
    await status.delete()
    
    if not all_songs:
        await call.message.answer("❌ Oxshash topilmadi")
        return
    
    display_songs = all_songs[:10]
    result = f"🔍 {search_query[:35]}\n\n"
    for idx, s in enumerate(display_songs, 1):
        if s['artist']:
            result += f"{idx}. {s['artist']} - {s['title'][:45]} {s['duration']}\n"
        else:
            result += f"{idx}. {s['title'][:50]} {s['duration']}\n"
    
    builder = InlineKeyboardBuilder()
    for idx, song in enumerate(display_songs, 1):
        song_id = hashlib.md5(song['url'].encode()).hexdigest()[:8]
        temp_data[song_id] = SongData(
            id=song_id, url=song['url'], title=song['full_title'],
            duration=song['duration'], artist=song['artist'], platform='youtube'
        )
        builder.button(text=f"{idx}", callback_data=f"dl_{song_id}")
    
    builder.adjust(5)
    await call.message.answer(
        f"{result}\n━━━━━━━━━━━━━━━━\n🔍 {len(all_songs)} ta\n━━━━━━━━━━━━━━━━\n👇 Raqamni bosing\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    song_id = call.data.replace("dl_", "")
    song_data = temp_data.get(song_id)
    
    if not song_data:
        await call.answer("❌", show_alert=True)
        return
    
    await call.answer("⏳")
    status = await call.message.answer(f"⏳ {song_data.title[:35]}...")
    
    try:
        filename, title = await asyncio.wait_for(download_mp3(song_data.url, call.from_user.id), timeout=90)
    except asyncio.TimeoutError:
        await status.delete()
        await call.message.answer("❌ Vaqt tugadi")
        return
    
    await status.delete()
    
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        artist, song_title = extract_artist_title(title)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 {song_title[:45]}\n📦 {format_size(file_size)}\n\n❤️ @zurnavolarbot",
            title=song_title[:60],
            performer=artist[:30] if artist else "Zurnavolar"
        )
        os.remove(filename)
        temp_data.pop(song_id, None)
    else:
        await call.message.answer(f"❌ {title[:100]}")

@dp.errors()
async def errors_handler(event, exception):
    if "message is not modified" not in str(exception).lower():
        logging.error(f"Xatolik: {exception}")
    return True

# =================== KEEP-ALIVE SERVER ===================
async def keep_alive_server():
    async def handle_client(reader, writer):
        try:
            await reader.read(8192)
            response_body = json.dumps({"status": "alive", "bot": "ZurnavolarBot", "uptime": str(int(time.time()))})
            response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(response_body)}\r\nConnection: close\r\n\r\n{response_body}"
            writer.write(response.encode())
            await writer.drain()
        except:
            pass
        finally:
            writer.close()
    
    server = await asyncio.start_server(handle_client, '0.0.0.0', Config.KEEP_ALIVE_PORT, reuse_address=True)
    print(f"🟢 Keep-Alive: {Config.KEEP_ALIVE_PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    ping_url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    async with aiohttp.ClientSession() as sess:
        while bot_running:
            try:
                async with sess.get(ping_url, timeout=10) as resp:
                    if resp.status == 200:
                        print(f"✅ Ping: {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(Config.PING_INTERVAL)

# =================== MAIN ===================
async def main():
    global bot_running
    logging.basicConfig(level=logging.INFO)
    
    print("🔄 Webhook tozalanmoqda...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook tozalandi")
    except Exception as e:
        print(f"⚠️ Webhook: {e}")
    
    await asyncio.sleep(2)
    
    if os.getenv("RAILWAY_ENVIRONMENT"):
        print("🚂 Railway muhiti")
    
    print(f"🍪 Cookie holati: {'✅ Mavjud' if COOKIE_FILE else '❌ Yoq'}")
    
    try:
        bot_info = await bot.get_me()
        print("=" * 45)
        print(f"🎵 Zurnavolar Bot: @{bot_info.username}")
        print(f"🆔 Bot ID: {bot_info.id}")
        print(f"🎬 FFmpeg: {'✅' if shutil.which('ffmpeg') else '❌'}")
        print("=" * 45)
    except Exception as e:
        print(f"❌ Bot xatosi: {e}")
        print("❗ Yangi token oling: @BotFather -> /newbot")
        return
    
    asyncio.create_task(keep_alive_server())
    asyncio.create_task(self_ping())
    
    while bot_running:
        try:
            print("🚀 Bot ishga tushdi...")
            await dp.start_polling(
                bot, 
                allowed_updates=['message', 'callback_query'],
                skip_updates=True
            )
        except Exception as e:
            err = str(e)
            if "Conflict" in err:
                print("⚠️ Conflict xatosi - 10 soniya keyin qayta...")
                await asyncio.sleep(10)
            else:
                print(f"❌ Xatolik: {e} — 5 soniya keyin...")
                await asyncio.sleep(5)

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
        print("\n⏹️ To'xtatildi!")
