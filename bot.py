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
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path
from collections import OrderedDict
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

# =================== LOGGING SETUP ===================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# =================== KONFIGURATSIYA ===================
load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    COOKIES_PATH = os.getenv("COOKIES_PATH", "cookies.txt")
    COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
    INSTAGRAM_COOKIES_PATH = os.getenv("INSTAGRAM_COOKIES_PATH", "instagram_cookies.txt")
    INSTAGRAM_COOKIES_CONTENT = os.getenv("INSTAGRAM_COOKIES_CONTENT", "")
    DOWNLOADS_PATH = Path(os.getenv("DOWNLOADS_PATH", "downloads"))
    TEMP_PATH = Path(os.getenv("TEMP_PATH", "temp_audio"))
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 50 * 1024 * 1024))
    AUDIO_SAMPLE_DURATION = int(os.getenv("AUDIO_SAMPLE_DURATION", 15))
    KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))
    PING_INTERVAL = int(os.getenv("PING_INTERVAL", 300))
    CACHE_EXPIRY = int(os.getenv("CACHE_EXPIRY", 3600))
    MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))
    SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", 30))
    SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", 180))
    MAX_AUDIO_DURATION = 600
    UPLOAD_TIMEOUT = 120

if not Config.BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable topilmadi!")

# =================== DEPENDENCIES CHECK ===================
def check_dependencies():
    missing = []
    if not shutil.which('ffmpeg'):
        missing.append('ffmpeg')
    if missing:
        logger.warning(f"⚠️ O'rnatilmagan: {', '.join(missing)}")
        return False
    logger.info("✅ Barcha dependencies o'rnatilgan")
    return True

check_dependencies()

# =================== CACHE BOSHQARUVI ===================
class ExpiringCache:
    def __init__(self, max_age: int = 3600):
        self.cache = OrderedDict()
        self.max_age = max_age
        self.lock = asyncio.Lock()

    async def set(self, key: str, value: dict):
        async with self.lock:
            self.cache[key] = {'data': value, 'timestamp': datetime.now()}
            await self._cleanup()

    async def get(self, key: str) -> Optional[dict]:
        async with self.lock:
            if key in self.cache:
                item = self.cache[key]
                if datetime.now() - item['timestamp'] < timedelta(seconds=self.max_age):
                    return item['data']
                else:
                    del self.cache[key]
            return None

    async def _cleanup(self):
        expired_keys = [
            k for k, v in self.cache.items()
            if datetime.now() - v['timestamp'] > timedelta(seconds=self.max_age)
        ]
        for k in expired_keys:
            del self.cache[k]

    async def clear_all(self):
        async with self.lock:
            self.cache.clear()

    async def size(self) -> int:
        async with self.lock:
            return len(self.cache)

# =================== COOKIE FAYL YARATISH ===================
def create_cookies_files():
    if Config.COOKIES_CONTENT:
        try:
            os.makedirs(os.path.dirname(Config.COOKIES_PATH) or '.', exist_ok=True)
            with open(Config.COOKIES_PATH, 'w', encoding='utf-8') as f:
                f.write(Config.COOKIES_CONTENT)
            logger.info(f"✅ YouTube cookie yaratildi: {Config.COOKIES_PATH}")
        except Exception as e:
            logger.error(f"YouTube cookie xatosi: {e}")

    if Config.INSTAGRAM_COOKIES_CONTENT:
        try:
            os.makedirs(os.path.dirname(Config.INSTAGRAM_COOKIES_PATH) or '.', exist_ok=True)
            with open(Config.INSTAGRAM_COOKIES_PATH, 'w', encoding='utf-8') as f:
                f.write(Config.INSTAGRAM_COOKIES_CONTENT)
            logger.info(f"✅ Instagram cookie yaratildi: {Config.INSTAGRAM_COOKIES_PATH}")
        except Exception as e:
            logger.error(f"Instagram cookie xatosi: {e}")

# =================== DATA MODELS ===================
@dataclass
class SongData:
    id: str
    url: str
    title: str
    duration: str = "0:00"
    artist: str = ""
    platform: str = 'youtube'
    duration_seconds: int = 0

# =================== INITIALIZATION ===================
Config.DOWNLOADS_PATH.mkdir(exist_ok=True, parents=True)
Config.TEMP_PATH.mkdir(exist_ok=True, parents=True)
logger.info("📁 Papkalar tayyorlandi")
create_cookies_files()

session = AiohttpSession(timeout=Config.SESSION_TIMEOUT)
bot = Bot(
    token=Config.BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS, thread_name_prefix="worker_")

temp_data: Dict[str, SongData] = {}
video_cache = ExpiringCache(max_age=Config.CACHE_EXPIRY)
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True
logger.info("🎵 Bot komponentlari yuklandi")

# =================== YORDAMCHI FUNKSIYALAR ===================
def get_platform(url: str) -> str:
    url_lower = url.lower()
    patterns = {
        'youtube': ['youtube.com', 'youtu.be', 'm.youtube.com'],
        'instagram': ['instagram.com', 'instagr.am', 'ig.me'],
        'tiktok': ['tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'],
        'facebook': ['facebook.com', 'fb.watch', 'fb.com'],
    }
    for platform, domains in patterns.items():
        if any(domain in url_lower for domain in domains):
            return platform
    return 'other'

def format_duration(seconds):
    if not seconds or seconds == 0:
        return "0:00"
    try:
        total_seconds = int(seconds)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        secs = total_seconds % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"
    except:
        return "0:00"

def format_size(bytes_size: int) -> str:
    if bytes_size is None or bytes_size == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

def extract_artist_title(full_title: str) -> tuple:
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

    clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title)
    remove_words = [
        'Official Video', 'Official Music Video', 'Official Audio',
        'MV', 'M/V', 'Music Video', 'Lyrics',
        'HD', '4K', '1080p', '720p', '360p',
        'TikTok', 'Trend', 'Viral',
        'Cover', 'AI Cover', 'Remix', 'Extended', 'Slowed',
        'Full Version', 'Full Video', 'Remastered',
    ]
    for word in remove_words:
        clean_title = re.sub(re.escape(word), '', clean_title, flags=re.IGNORECASE)

    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
    clean_artist = re.sub(r'\s+', ' ', clean_artist).strip()

    if not clean_title or len(clean_title) < 3:
        clean_title = title.strip()

    return clean_artist, clean_title

def get_cookies_for_platform(platform: str = 'youtube') -> dict:
    opts = {}
    if platform == 'instagram' and os.path.exists(Config.INSTAGRAM_COOKIES_PATH):
        opts['cookiefile'] = Config.INSTAGRAM_COOKIES_PATH
    elif os.path.exists(Config.COOKIES_PATH):
        opts['cookiefile'] = Config.COOKIES_PATH
    return opts

def get_ydl_opts(output_path: str, format_type: str = 'video', platform: str = 'youtube') -> dict:
    opts = {
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
        'fragment_retries': 3,
        'socket_timeout': Config.SOCKET_TIMEOUT,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    }

    if shutil.which('node'):
        opts['js_runtimes'] = {'node': {}}
        opts['remote_components'] = ['ejs:github']

    if format_type == 'video':
        opts.update({
            'format': 'best[height<=720][ext=mp4]/best[ext=mp4]',
            'merge_output_format': 'mp4',
        })
    elif format_type == 'audio':
        opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })

    opts.update(get_cookies_for_platform(platform))
    return opts

# =================== AUDIO ANIQLASH ===================
async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    if not SHAZAM_AVAILABLE or not shazam:
        return None
    try:
        if not os.path.exists(video_path):
            return None
        audio_path = video_path.replace('.mp4', '_sample.mp3').replace('.webm', '_sample.mp3')
        cmd = [
            'ffmpeg', '-i', video_path, '-ss', '5', '-t', str(Config.AUDIO_SAMPLE_DURATION),
            '-q:a', '0', '-map', 'a', audio_path, '-y', '-loglevel', 'quiet'
        ]
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(pool, lambda: subprocess.run(cmd, capture_output=True, text=True))
        if result.returncode != 0 or not os.path.exists(audio_path):
            return None
        try:
            shazam_result = await asyncio.wait_for(shazam.recognize(audio_path), timeout=20)
        except:
            shazam_result = None
        finally:
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except:
                    pass
        if shazam_result and 'track' in shazam_result:
            track = shazam_result['track']
            return {
                'title': track.get('title', ''),
                'artist': track.get('subtitle', ''),
                'full_title': f"{track.get('subtitle', '')} - {track.get('title', '')}",
            }
        return None
    except:
        return None

# =================== VIDEO YUKLASH ===================
async def download_video(url: str, user_id: int):
    def run():
        try:
            platform = get_platform(url)
            output_path = str(Config.DOWNLOADS_PATH / f"video_{user_id}_{int(time.time())}_%(title)s.%(ext)s")
            opts = get_ydl_opts(output_path, 'video', platform)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    base = filename.rsplit('.', 1)[0]
                    for ext in ['.mp4', '.webm', '.mkv', '.mov']:
                        if os.path.exists(base + ext):
                            filename = base + ext
                            break
                return filename, info.get('title', 'Video'), info.get('duration', 0)
        except Exception as e:
            return None, str(e), 0
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== MP3 YUKLASH ===================
async def download_mp3(url: str, user_id: int):
    def run():
        try:
            platform = get_platform(url)
            check_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': False}
            check_opts.update(get_cookies_for_platform(platform))
            if shutil.which('node'):
                check_opts['js_runtimes'] = {'node': {}}
                check_opts['remote_components'] = ['ejs:github']
            with yt_dlp.YoutubeDL(check_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get('duration', 0) > Config.MAX_AUDIO_DURATION:
                    return None, f"VIDEO_JUDA_UZUN:{info['duration']}"
            output_path = str(Config.DOWNLOADS_PATH / f"audio_{user_id}_{int(time.time())}_%(title)s.%(ext)s")
            opts = get_ydl_opts(output_path, 'audio', platform)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                return filename, info.get('title', 'Audio')
        except Exception as e:
            return None, str(e)
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== QO'SHIQ QIDIRISH ===================
async def search_songs(query: str, limit: int = 10) -> List[dict]:
    def run():
        try:
            opts = get_ydl_opts('', 'video', 'youtube')
            opts.update({'quiet': True, 'no_warnings': True, 'extract_flat': True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                songs = []
                if 'entries' in info:
                    for i, item in enumerate(info['entries'], 1):
                        if item and item.get('id'):
                            full_title = item.get('title', 'Nomalum')
                            artist, title = extract_artist_title(full_title)
                            dur = item.get('duration', 0) or 0
                            songs.append({
                                'number': i,
                                'title': title[:60],
                                'artist': artist[:40],
                                'full_title': full_title[:80],
                                'duration': format_duration(dur),
                                'duration_seconds': dur,
                                'url': f"https://youtube.com/watch?v={item['id']}",
                            })
                return songs
        except:
            return []
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== CLEANUP ===================
async def cleanup_old_files():
    while bot_running:
        try:
            await asyncio.sleep(1800)
            for directory in [Config.DOWNLOADS_PATH, Config.TEMP_PATH]:
                if directory.exists():
                    for f in directory.glob('*'):
                        if f.is_file() and time.time() - f.stat().st_mtime > 1800:
                            try:
                                f.unlink()
                            except:
                                pass
        except:
            pass

# =================== HANDLERS ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    try:
        user_name = message.from_user.first_name or "Foydalanuvchi"
        await message.answer(
            f"🎵 <b>Salom, {user_name}!</b> 🎵\n\n"
            "📥 Link yuboring:\n🎬 YouTube | 📸 Instagram | 🎵 TikTok | 📘 Facebook\n\n"
            "🔍 Qo'shiq qidirish: <code>shoxruxon</code>\n\n"
            "🎯 Instagram/TikTok video yuborsangiz, qo'shiqni aniqlaydi!\n\n"
            "⚠️ MP3: ≤10 daqiqa\n\n"
            "/help | /about\n❤️ @zurnavolarbot"
        )
    except:
        pass

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Yordam</b>\n\n"
        "1️⃣ Link yuboring\n2️⃣ Qo'shiq nomini yozing\n3️⃣ Instagram/TikTok video = avtoaniqlash\n\n"
        "/start | /help | /about\n\n"
        "✅ MP3 192kbps | ✅ Video 720p | ✅ Shazam\n"
        f"⚠️ MP3 ≤10min | Fayl ≤{format_size(Config.MAX_FILE_SIZE)}\n\n❤️ @zurnavolarbot"
    )

@dp.message(Command("about"))
async def cmd_about(message: Message):
    try:
        bot_info = await bot.get_me()
        cache_size = await video_cache.size()
        shazam_status = "✅" if SHAZAM_AVAILABLE else "❌"
        ffmpeg_status = "✅" if shutil.which('ffmpeg') else "❌"
        node_status = "✅" if shutil.which('node') else "❌"
        yt_cookie = "✅" if os.path.exists(Config.COOKIES_PATH) else "❌"
        ig_cookie = "✅" if os.path.exists(Config.INSTAGRAM_COOKIES_PATH) else "❌"
        description = bot_info.description or "Qo'shiq va video yuklagich"

        await message.answer(
            f"ℹ️ <b>Bot haqida</b>\n\n"
            f"👤 {bot_info.first_name} | @{bot_info.username}\n"
            f"📝 {description}\n\n"
            f"🛠️ Shazam:{shazam_status} FFmpeg:{ffmpeg_status} Node:{node_status}\n"
            f"🍪 YT:{yt_cookie} IG:{ig_cookie}\n\n"
            f"📊 Cache:{cache_size} | MP3≤10min | ≤{format_size(Config.MAX_FILE_SIZE)}\n\n"
            "❤️ @zurnavolarbot"
        )
    except:
        pass

@dp.message(F.text)
async def handle_message(message: Message):
    try:
        text = message.text.strip()
        user_id = message.from_user.id
        if re.match(r'^https?://', text):
            await process_url(message, text, user_id)
        else:
            await process_search(message, text, user_id)
    except:
        await message.answer("❌ Xatolik!")

async def process_url(message: Message, url: str, user_id: int):
    try:
        platform = get_platform(url)
        if platform == 'other':
            await message.answer("❌ Faqat: YouTube | Instagram | TikTok | Facebook")
            return

        status = await message.answer("⏳ Yuklanmoqda...")
        filename, full_title, duration = await download_video(url, user_id)
        await status.delete()

        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                if file_size > Config.MAX_FILE_SIZE:
                    await message.answer(f"❌ Juda katta!\n📦 {format_size(file_size)} > {format_size(Config.MAX_FILE_SIZE)}")
                    os.remove(filename)
                    return

                url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
                artist, title = extract_artist_title(full_title)

                # Audio aniqlashni backgroundda
                identified_song = None
                if platform in ['instagram', 'tiktok', 'facebook']:
                    identified_song = await identify_audio_from_video(filename)

                if identified_song:
                    search_title = identified_song['full_title']
                    search_artist = identified_song['artist']
                else:
                    search_title = full_title
                    search_artist = artist

                await video_cache.set(url_hash, {
                    'url': url, 'title': full_title, 'artist': search_artist,
                    'clean_title': title, 'duration': duration, 'platform': platform,
                    'identified_song': identified_song, 'search_query': search_title,
                })

                # Tugmalar ixcham
                if duration <= Config.MAX_AUDIO_DURATION:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3_{url_hash}"),
                         InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"similar_{url_hash}")]
                    ])
                else:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"similar_{url_hash}")]
                    ])

                platform_emoji = {'youtube': '🎬', 'instagram': '📸', 'tiktok': '🎵', 'facebook': '📘'}
                video_file = FSInputFile(filename)

                caption = f"{platform_emoji.get(platform, '📹')} <b>{full_title[:50]}</b>\n⏱️ {format_duration(duration)}"
                if identified_song:
                    caption += f"\n🎯 {identified_song['full_title'][:80]}"
                if duration > Config.MAX_AUDIO_DURATION:
                    m, s = duration // 60, duration % 60
                    caption += f"\n⚠️ MP3 yo'q ({m}:{s:02d} > 10:00)"
                caption += f"\n\n❤️ @zurnavolarbot"

                # Tezkor yuborish
                try:
                    await asyncio.wait_for(
                        message.answer_video(video_file, caption=caption, reply_markup=keyboard),
                        timeout=Config.UPLOAD_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    await message.answer(f"⚠️ Video yuklandi, lekin yuborishda timeout!\n📦 {format_size(file_size)}\n💡 Kichikroq video tanlang.")
                finally:
                    if os.path.exists(filename):
                        os.remove(filename)
            except Exception as e:
                logger.error(f"Video jo'natishda xatolik: {e}")
                await message.answer("❌ Video jo'natishda xatolik!")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            error_text = str(full_title)[:200] if full_title else "Nomalum xatolik"
            if "login required" in error_text.lower():
                await message.answer("❌ Instagram login talab qilmoqda!\n💡 Instagram cookie qo'shing yoki YouTube link yuboring.")
            else:
                await message.answer(f"❌ Yuklab bo'lmadi!\n📝 {error_text[:150]}")
    except Exception as e:
        logger.error(f"URL processing xatosi: {e}")
        await message.answer("❌ URL'ni qayta tekshiring!")

async def process_search(message: Message, query: str, user_id: int):
    try:
        if len(query) < 2:
            await message.answer("❌ Kamida 2 ta harf!")
            return

        status = await message.answer(f"🔍 <b>{query}</b>...")
        songs = await search_songs(query, limit=10)
        await status.delete()

        if not songs:
            await message.answer(f"❌ <code>{query}</code> topilmadi!")
            return

        # Ixcham jadval
        songs_text = ""
        for s in songs:
            dur = s['duration']
            if s.get('duration_seconds', 0) > Config.MAX_AUDIO_DURATION:
                dur = f"🔴{s['duration']}"
            songs_text += f"<code>{s['number']:>2}</code> {s['artist']} — {s['title']}  <code>{dur}</code>\n"

        builder = InlineKeyboardBuilder()
        for song in songs:
            song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
            temp_data[song_id] = SongData(
                id=song_id, url=song['url'], title=song['full_title'],
                duration=song['duration'], artist=song['artist'],
                platform='youtube', duration_seconds=song.get('duration_seconds', 0)
            )
            builder.button(text=f"{song['number']}", callback_data=f"dl_{song_id}")

        builder.adjust(5)
        await message.answer(
            f"🎵 <b>{query}</b>\n\n{songs_text}\n👇 Raqamni bosing | 🔴 = 10min+\n❤️ @zurnavolarbot",
            reply_markup=builder.as_markup()
        )
    except:
        await message.answer("❌ Qidirishda xatolik!")

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    try:
        url_hash = call.data.replace("mp3_", "")
        video_info = await video_cache.get(url_hash)
        if not video_info:
            await call.answer("❌ Topilmadi!", show_alert=True)
            return

        duration = video_info.get('duration', 0)
        if duration > Config.MAX_AUDIO_DURATION:
            m, s = duration // 60, duration % 60
            await call.answer(f"❌ {m}:{s:02d} > 10:00", show_alert=True)
            return

        await call.answer("⏳ MP3 yuklanmoqda...")
        display = video_info.get('identified_song', {}).get('full_title', video_info['title'])[:50]
        status = await call.message.answer(f"⏳ <b>{display}</b>...")

        filename, title = await download_mp3(video_info['url'], call.from_user.id)
        await status.delete()

        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                artist, song_title = extract_artist_title(title)
                await call.message.answer_audio(
                    FSInputFile(filename),
                    caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n❤️ @zurnavolarbot",
                    title=song_title[:64],
                    performer=artist[:64] if artist else "Zurnavolar"
                )
                os.remove(filename)
            except:
                await call.message.answer("❌ Audio jo'natishda xatolik!")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            error_msg = str(title)
            if error_msg.startswith("VIDEO_JUDA_UZUN:"):
                dur = int(error_msg.split(":")[1])
                m, s = dur // 60, dur % 60
                await call.message.answer(f"❌ {m}:{s:02d} > 10:00")
            else:
                await call.message.answer(f"❌ {error_msg[:100]}")
    except:
        await call.answer("❌ Xatolik!", show_alert=True)

@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    try:
        url_hash = call.data.replace("similar_", "")
        video_info = await video_cache.get(url_hash)
        if not video_info:
            await call.answer("❌ Topilmadi!", show_alert=True)
            return

        await call.answer("🔍 Qidirilmoqda...")

        if video_info.get('identified_song'):
            search_query = video_info['identified_song']['full_title']
            artist = video_info['identified_song'].get('artist', '')
            song_title = video_info['identified_song'].get('title', '')
        else:
            artist = video_info.get('artist', '')
            song_title = video_info.get('clean_title', '')
            search_query = f"{artist} {song_title}".strip()

        status = await call.message.answer(f"🔍 <b>{search_query[:60]}</b>...")

        all_songs = []
        seen_urls = set()

        if search_query:
            for s in await search_songs(search_query, limit=10):
                if s['url'] not in seen_urls:
                    all_songs.append(s)
                    seen_urls.add(s['url'])

        if song_title:
            for q in [f"{song_title} cover version", f"{song_title} remix"]:
                for s in await search_songs(q, limit=5):
                    if s['url'] not in seen_urls:
                        all_songs.append(s)
                        seen_urls.add(s['url'])

        if len(all_songs) < 5 and artist:
            for s in await search_songs(artist, limit=5):
                if s['url'] not in seen_urls:
                    all_songs.append(s)
                    seen_urls.add(s['url'])

        await status.delete()

        if not all_songs:
            await call.message.answer("❌ Topilmadi!")
            return

        display_songs = all_songs[:10]
        songs_text = ""
        for idx, s in enumerate(display_songs, 1):
            dur = s['duration']
            if s.get('duration_seconds', 0) > Config.MAX_AUDIO_DURATION:
                dur = f"🔴{s['duration']}"
            songs_text += f"<code>{idx:>2}</code> {s['artist']} — {s['title'][:50]}  <code>{dur}</code>\n"

        builder = InlineKeyboardBuilder()
        for idx, song in enumerate(display_songs, 1):
            song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
            temp_data[song_id] = SongData(
                id=song_id, url=song['url'], title=song['full_title'],
                duration=song['duration'], artist=song['artist'],
                platform='youtube', duration_seconds=song.get('duration_seconds', 0)
            )
            builder.button(text=f"{idx}", callback_data=f"dl_{song_id}")

        builder.adjust(5)
        await call.message.answer(
            f"🎵 <b>{search_query[:50]}</b>\n\n{songs_text}\n"
            f"━━━ 🔍{len(all_songs)}ta | 🔴10min+ ━━━\n👇 Raqamni bosing\n❤️ @zurnavolarbot",
            reply_markup=builder.as_markup()
        )
    except:
        await call.answer("❌ Xatolik!", show_alert=True)

@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    try:
        song_id = call.data.replace("dl_", "")
        song_data = temp_data.get(song_id)
        if not song_data:
            await call.answer("❌ Topilmadi!", show_alert=True)
            return

        if song_data.duration_seconds > Config.MAX_AUDIO_DURATION:
            m, s = song_data.duration_seconds // 60, song_data.duration_seconds % 60
            await call.answer(f"❌ {m}:{s:02d} > 10:00", show_alert=True)
            return

        await call.answer("⏳ MP3 yuklanmoqda...")
        status = await call.message.answer(f"⏳ <b>{song_data.title[:40]}</b>...")

        filename, title = await download_mp3(song_data.url, call.from_user.id)
        await status.delete()

        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                artist, song_title = extract_artist_title(title)
                await call.message.answer_audio(
                    FSInputFile(filename),
                    caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n❤️ @zurnavolarbot",
                    title=song_title[:64],
                    performer=artist[:64] if artist else "Zurnavolar"
                )
                os.remove(filename)
                temp_data.pop(song_id, None)
            except:
                await call.message.answer("❌ Audio jo'natishda xatolik!")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            error_msg = str(title)
            if error_msg.startswith("VIDEO_JUDA_UZUN:"):
                dur = int(error_msg.split(":")[1])
                m, s = dur // 60, dur % 60
                await call.message.answer(f"❌ {m}:{s:02d} > 10:00")
            else:
                await call.message.answer(f"❌ {error_msg[:100]}")
    except:
        await call.answer("❌ Xatolik!", show_alert=True)

@dp.errors()
async def errors_handler(event, exception):
    if "message is not modified" not in str(exception).lower():
        logger.error(f"Bot xatosi: {exception}")
    return True

# =================== KEEP-ALIVE SERVER ===================
async def keep_alive_server():
    async def handle_client(reader, writer):
        try:
            await reader.read(8192)
            response_body = json.dumps({
                "status": "alive", "bot": "ZurnavolarBot",
                "uptime": str(int(time.time())), "timestamp": datetime.now().isoformat()
            })
            response = (
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\nConnection: close\r\n\r\n{response_body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except:
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(handle_client, '0.0.0.0', Config.KEEP_ALIVE_PORT, reuse_address=True)
        logger.info(f"🟢 Keep-Alive: 0.0.0.0:{Config.KEEP_ALIVE_PORT}")
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.error(f"Server xatosi: {e}")

async def self_ping():
    await asyncio.sleep(30)
    ping_url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    async with aiohttp.ClientSession() as session:
        while bot_running:
            try:
                async with session.get(ping_url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.debug(f"✅ Ping OK: {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(Config.PING_INTERVAL)

# =================== MAIN ===================
async def main():
    global bot_running
    logger.info("=" * 50)
    logger.info("🎵 ZURNAVOLAR BOT ISHGA TUSHUVELYAPTI...")
    logger.info("=" * 50)

    try:
        bot_info = await bot.get_me()
        yt_cookie = "✅" if os.path.exists(Config.COOKIES_PATH) else "❌"
        ig_cookie = "✅" if os.path.exists(Config.INSTAGRAM_COOKIES_PATH) else "❌"
        node_status = "✅" if shutil.which('node') else "❌"

        logger.info(f"✅ Bot: @{bot_info.username} | {bot_info.first_name}")
        logger.info(f"🎤 Shazam: {'✅' if SHAZAM_AVAILABLE else '❌'}")
        logger.info(f"🔧 FFmpeg: {'✅' if shutil.which('ffmpeg') else '❌'}")
        logger.info(f"🟢 Node.js: {node_status}")
        logger.info(f"🍪 YT: {yt_cookie} | IG: {ig_cookie}")
        logger.info(f"⏱️ MP3: ≤10 daqiqa | ⏫ Upload timeout: {Config.UPLOAD_TIMEOUT}s")
    except Exception as e:
        logger.error(f"Bot info xatosi: {e}")

    logger.info("=" * 50)

    asyncio.create_task(keep_alive_server())
    asyncio.create_task(self_ping())
    asyncio.create_task(cleanup_old_files())

    while bot_running:
        try:
            logger.info("🚀 Bot polling ishga tushdi...")
            await dp.start_polling(bot, allowed_updates=['message', 'callback_query'], skip_updates=False)
        except Exception as e:
            logger.error(f"❌ Polling xatosi: {e}")
            if bot_running:
                await asyncio.sleep(10)

def signal_handler(sig, frame):
    global bot_running
    logger.info("\n⏹️ To'xtatilmoqda...")
    bot_running = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ To'xtatildi!")
    except Exception as e:
        logger.error(f"Fatal xatolik: {e}")
        sys.exit(1)
