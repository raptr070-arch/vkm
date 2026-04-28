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
    print("⚠️ ShazamIO o'rnatilmagan: pip install shazamio")

# =================== LOGGING SETUP ===================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =================== KONFIGURATSIYA ===================
load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    COOKIES_PATH = os.getenv("COOKIES_PATH", "cookies.txt")
    COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
    DOWNLOADS_PATH = Path(os.getenv("DOWNLOADS_PATH", "downloads"))
    TEMP_PATH = Path(os.getenv("TEMP_PATH", "temp_audio"))
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 50 * 1024 * 1024))
    AUDIO_SAMPLE_DURATION = int(os.getenv("AUDIO_SAMPLE_DURATION", 15))
    KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))
    PING_INTERVAL = int(os.getenv("PING_INTERVAL", 300))
    CACHE_EXPIRY = int(os.getenv("CACHE_EXPIRY", 3600))
    MAX_WORKERS = int(os.getenv("MAX_WORKERS", 5))
    SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", 30))
    SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", 60))

if not Config.BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable topilmadi!")

# =================== DEPENDENCIES CHECK ===================
def check_dependencies():
    """Barcha kerakli dependencylarni tekshirish"""
    dependencies = {
        'ffmpeg': 'FFmpeg',
        'yt-dlp': 'yt-dlp (pip install yt-dlp)',
    }
    
    missing = []
    
    # FFmpeg tekshirish
    if not shutil.which('ffmpeg'):
        missing.append('ffmpeg')
    
    for dep, name in dependencies.items():
        if dep == 'ffmpeg':
            continue
        try:
            __import__(dep.replace('-', '_'))
        except ImportError:
            missing.append(dep)
    
    if missing:
        logger.warning(f"⚠️ O'rnatilmagan: {', '.join(missing)}")
        return False
    
    logger.info("✅ Barcha dependencies o'rnatilgan")
    return True

check_dependencies()

# =================== CACHE BOSHQARUVI ===================
class ExpiringCache:
    """Vaqti tugagan cache'ni avtomatik o'chiradi"""
    def __init__(self, max_age: int = 3600):
        self.cache = OrderedDict()
        self.max_age = max_age
        self.lock = asyncio.Lock()
    
    async def set(self, key: str, value: dict):
        async with self.lock:
            self.cache[key] = {
                'data': value,
                'timestamp': datetime.now()
            }
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
        """Vaqti tugagan itemlarni o'chirish"""
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
def create_cookies_file():
    """Environment variable'dan cookie fayl yaratish"""
    if Config.COOKIES_CONTENT:
        try:
            os.makedirs(os.path.dirname(Config.COOKIES_PATH) or '.', exist_ok=True)
            with open(Config.COOKIES_PATH, 'w', encoding='utf-8') as f:
                f.write(Config.COOKIES_CONTENT)
            file_size = os.path.getsize(Config.COOKIES_PATH)
            logger.info(f"✅ Cookie fayl yaratildi: {Config.COOKIES_PATH} ({file_size} bayt)")
            return True
        except Exception as e:
            logger.error(f"❌ Cookie fayl yaratishda xatolik: {e}")
            return False
    else:
        logger.warning("⚠️ COOKIES_CONTENT environment variable topilmadi")
        return False

# =================== DATA MODELS ===================
@dataclass
class SongData:
    id: str
    url: str
    title: str
    duration: str = "0:00"
    artist: str = ""
    platform: str = 'youtube'

# =================== INITIALIZATION ===================
Config.DOWNLOADS_PATH.mkdir(exist_ok=True, parents=True)
Config.TEMP_PATH.mkdir(exist_ok=True, parents=True)

logger.info("📁 Papkalar tayyorlandi")

# Cookie faylni yaratish
create_cookies_file()

session = AiohttpSession(timeout=Config.SESSION_TIMEOUT)
bot = Bot(
    token=Config.BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
pool = ThreadPoolExecutor(
    max_workers=Config.MAX_WORKERS,
    thread_name_prefix="worker_"
)

temp_data: Dict[str, SongData] = {}
video_cache = ExpiringCache(max_age=Config.CACHE_EXPIRY)
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True

logger.info("🎵 Bot kompponentlari yuklandy")

# =================== YORDAMCHI FUNKSIYALAR ===================
def get_platform(url: str) -> str:
    """URL platformasini aniqlash"""
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
    """Vaqtni MM:SS formatiga o'tkazish"""
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
    except (TypeError, ValueError):
        return "0:00"

def format_size(bytes_size: int) -> str:
    """Fayl hajmini o'qilgan formatga o'tkazish"""
    if bytes_size is None or bytes_size == 0:
        return "0 B"
    
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

def extract_artist_title(full_title: str) -> tuple:
    """Sarlavhadan artist va sharkni ajratish"""
    if not full_title:
        return "", ""
    
    # Artist va sharkni ajratish
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
    
    # Ortiqcha informatsiyani olib tashlash
    clean_title = title
    clean_title = re.sub(r'\(.*?\)', '', clean_title)
    clean_title = re.sub(r'\[.*?\]', '', clean_title)
    
    remove_words = [
        'Official Video', 'Official Music Video', 'Official Audio',
        'MV', 'M/V', 'Music Video', 'Lyrics',
        'HD', '4K', '1080p', '720p', '360p',
        'TikTok', 'Trend', 'Viral',
        'Cover', 'AI Cover', 'Remix', 'Extended', 'Slowed',
        'Full Version', 'Full Video', 'Remastered',
    ]
    
    for word in remove_words:
        clean_title = re.sub(
            re.escape(word),
            '',
            clean_title,
            flags=re.IGNORECASE
        )
    
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
    clean_artist = re.sub(r'\s+', ' ', clean_artist).strip()
    
    if not clean_title or len(clean_title) < 3:
        clean_title = title.strip()
    
    return clean_artist, clean_title

def get_cookies_opts() -> dict:
    """Cookie options'ni qaytarish"""
    opts = {}
    if os.path.exists(Config.COOKIES_PATH):
        try:
            opts['cookiefile'] = Config.COOKIES_PATH
            logger.info(f"✅ Cookie ishlatilmoqda: {Config.COOKIES_PATH}")
        except Exception as e:
            logger.warning(f"Cookie xatosi: {e}")
    else:
        logger.warning("⚠️ Cookie faylsiz ishlash")
    return opts

def get_ydl_opts(output_path: str, format_type: str = 'video') -> dict:
    """YoutubeDL options'ni qaytarish"""
    opts = {
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'retries': 3,
        'socket_timeout': Config.SOCKET_TIMEOUT,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    }
    
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
    
    opts.update(get_cookies_opts())
    return opts

# =================== AUDIO ANIQLASH ===================
async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    """Shazam yordamida audio'ni aniqlash"""
    if not SHAZAM_AVAILABLE or not shazam:
        logger.warning("Shazam mavjud emas")
        return None
    
    try:
        if not os.path.exists(video_path):
            logger.error(f"Video fayl topilmadi: {video_path}")
            return None
        
        audio_path = video_path.replace('.mp4', '_sample.mp3').replace('.webm', '_sample.mp3')
        
        cmd = [
            'ffmpeg', '-i', video_path,
            '-ss', '5',
            '-t', str(Config.AUDIO_SAMPLE_DURATION),
            '-q:a', '0',
            '-map', 'a',
            audio_path,
            '-y', '-loglevel', 'quiet'
        ]
        
        def run_ffmpeg():
            return subprocess.run(cmd, capture_output=True, text=True)
        
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(pool, run_ffmpeg)
        
        if result.returncode != 0:
            logger.error(f"FFmpeg xatosi: {result.stderr}")
            return None
        
        if not os.path.exists(audio_path):
            logger.warning("Audio namunas yaratilmadi")
            return None
        
        try:
            shazam_result = await asyncio.wait_for(
                shazam.recognize(audio_path),
                timeout=30
            )
        except asyncio.TimeoutError:
            logger.warning("Shazam vaqti tugadi")
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
    except Exception as e:
        logger.error(f"Audio aniqlashda xatolik: {e}")
        return None

# =================== VIDEO YUKLASH ===================
async def download_video(url: str, user_id: int):
    """YouTube, Instagram, TikTok, Facebook dan video yuklash"""
    def run():
        try:
            output_path = str(Config.DOWNLOADS_PATH / f"video_{user_id}_{int(time.time())}_%(title)s.%(ext)s")
            opts = get_ydl_opts(output_path, 'video')
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                logger.info(f"Video yuklanmoqda: {url}")
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                
                if not os.path.exists(filename):
                    base = filename.rsplit('.', 1)[0]
                    for ext in ['.mp4', '.webm', '.mkv', '.mov']:
                        test_path = base + ext
                        if os.path.exists(test_path):
                            filename = test_path
                            break
                
                full_title = info.get('title', 'Video')
                duration = info.get('duration', 0)
                logger.info(f"Video yuklandi: {filename}")
                return filename, full_title, duration
        except Exception as e:
            logger.error(f"Video yuklashda xatolik: {e}")
            return None, str(e), 0
    
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(pool, run)

# =================== MP3 YUKLASH ===================
async def download_mp3(url: str, user_id: int):
    """Audio yuklash va MP3 ga o'tkazish"""
    def run():
        try:
            output_path = str(Config.DOWNLOADS_PATH / f"audio_{user_id}_{int(time.time())}_%(title)s.%(ext)s")
            opts = get_ydl_opts(output_path, 'audio')
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                logger.info(f"Audio yuklanmoqda: {url}")
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                title = info.get('title', 'Audio')
                logger.info(f"Audio yuklandi: {filename}")
                return filename, title
        except Exception as e:
            logger.error(f"Audio yuklashda xatolik: {e}")
            return None, str(e)
    
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(pool, run)

# =================== QO'SHIQ QIDIRISH ===================
async def search_songs(query: str, limit: int = 10) -> List[dict]:
    """YouTube da qo'shiq qidirish"""
    def run():
        try:
            opts = get_ydl_opts('', 'video')
            opts.update({
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
            })
            
            search_query = f"ytsearch{limit}:{query}"
            logger.info(f"Qidirilmoqda: {query}")
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                songs = []
                
                if 'entries' in info:
                    for i, item in enumerate(info['entries'], 1):
                        if item:
                            full_title = item.get('title', 'Nomalum')
                            artist, title = extract_artist_title(full_title)
                            video_id = item.get('id', '')
                            
                            if not video_id:
                                continue
                            
                            songs.append({
                                'number': i,
                                'title': title[:60],
                                'artist': artist[:40],
                                'full_title': full_title[:80],
                                'duration': format_duration(item.get('duration', 0)),
                                'url': f"https://youtube.com/watch?v={video_id}",
                            })
                
                logger.info(f"Topildi: {len(songs)} ta qo'shiq")
                return songs
        except Exception as e:
            logger.error(f"Qidirishda xatolik: {e}")
            return []
    
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(pool, run)

# =================== CLEANUP ===================
async def cleanup_old_files():
    """Eski fayllarni o'chirish (1 soatdan eski)"""
    while bot_running:
        try:
            await asyncio.sleep(3600)  # 1 soat
            
            for directory in [Config.DOWNLOADS_PATH, Config.TEMP_PATH]:
                if directory.exists():
                    for file_path in directory.glob('*'):
                        if file_path.is_file():
                            file_age = time.time() - file_path.stat().st_mtime
                            if file_age > 3600:  # 1 soat
                                try:
                                    file_path.unlink()
                                    logger.info(f"Eski fayl o'chirildi: {file_path}")
                                except Exception as e:
                                    logger.error(f"Fayl o'chirishda xatolik: {e}")
        except Exception as e:
            logger.error(f"Cleanup oshib ketdi: {e}")

# =================== HANDLERS ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Start command"""
    try:
        user_name = message.from_user.first_name or "Foydalanuvchi"
        await message.answer(
            f"🎵 <b>Salom, {user_name}!</b> 🎵\n\n"
            "👋 <b>Zurnavolar Bot</b>'ga xush kelibsiz!\n\n"
            "📥 <b>Link yuboring:</b>\n"
            "🎬 YouTube | 📸 Instagram | 🎵 TikTok | 📘 Facebook\n\n"
            "🔍 <b>Qo'shiq qidirish:</b>\n"
            "Masalan: <code>yalla</code>, <code>shoxruxon</code>\n\n"
            "🎯 <b>Instagram/TikTok video</b> yuborsangiz,\n"
            "video ichidagi qo'shiqni avtomatik aniqlaydi! 🎤\n\n"
            "📞 <b>Buyruqlar:</b>\n"
            "/help - Yordam\n"
            "/about - Bot haqida\n\n"
            "❤️ @zurnavolarbot"
        )
    except Exception as e:
        logger.error(f"Start commandda xatolik: {e}")
        await message.answer("❌ Xatolik yuz berdi!")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Help command"""
    try:
        await message.answer(
            "📖 <b>Yordam</b>\n\n"
            "🎵 <b>Qanday ishlatiladi?</b>\n"
            "1️⃣ YouTube/Instagram/TikTok/Facebook linkini yuboring\n"
            "2️⃣ Qo'shiq nomini yozib qidiring\n"
            "3️⃣ Instagram/TikTok video yuborsangiz, avtomatik qo'shiq aniqlanadi\n\n"
            "⚙️ <b>Buyruqlar:</b>\n"
            "/start - Botni qayta ishga tushirish\n"
            "/help - Yordam\n"
            "/about - Bot haqida\n\n"
            "📌 <b>Xususiyatlar:</b>\n"
            "✅ MP3 yuklash (192kbps)\n"
            "✅ Video yuklash (720p)\n"
            "✅ Qo'shiq qidirish\n"
            "✅ Audio aniqlash (Shazam) 🎤\n"
            "✅ Oxshash qo'shiqlar\n"
            "✅ Tez va ishonchli\n\n"
            "⚠️ <b>Cheklovlar:</b>\n"
            f"• Fayl maksimal hajmi: {format_size(Config.MAX_FILE_SIZE)}\n"
            "• Qidiruv: 10 ta natija\n"
            "• Cache: 1 soat\n\n"
            "❤️ @zurnavolarbot"
        )
    except Exception as e:
        logger.error(f"Help commandda xatolik: {e}")

@dp.message(Command("about"))
async def cmd_about(message: Message):
    """About command"""
    try:
        bot_info = await bot.get_me()
        cache_size = await video_cache.size()
        
        await message.answer(
            "ℹ️ <b>Bot haqida</b>\n\n"
            f"👤 <b>Nomi:</b> {bot_info.first_name}\n"
            f"🤖 <b>Username:</b> @{bot_info.username}\n"
            f"📝 <b>Izoh:</b> {bot_info.description or 'Qo'shiq va video yuklagich'}\n\n"
            "🛠️ <b>Texnologiyalar:</b>\n"
            "• Python 3.9+\n"
            "• aiogram 3.x\n"
            "• yt-dlp\n"
            f"• Shazam: {'✅ Faol' if SHAZAM_AVAILABLE else '❌ O'chirilgan'}\n"
            f"• FFmpeg: {'✅ Faol' if shutil.which('ffmpeg') else '❌ O'chirilgan'}\n\n"
            "📊 <b>Statistika:</b>\n"
            f"• Cache hajmi: {cache_size} ta item\n"
            f"• Vaqti tugash: {Config.CACHE_EXPIRY} soniya\n\n"
            "📧 <b>Bog'lanish:</b>\n"
            "@zurnavolarbot\n\n"
            "❤️ Botni yoqtirsangiz, do'stlaringizga ulashing!"
        )
    except Exception as e:
        logger.error(f"About commandda xatolik: {e}")

@dp.message(F.text)
async def handle_message(message: Message):
    """Asosiy message handler"""
    try:
        text = message.text.strip()
        user_id = message.from_user.id
        
        if re.match(r'^https?://', text):
            await process_url(message, text, user_id)
        else:
            await process_search(message, text, user_id)
    except Exception as e:
        logger.error(f"Message handlerda xatolik: {e}")
        await message.answer("❌ Xatolik yuz berdi! Iltimos, qayta urinib ko'ring.")

async def process_url(message: Message, url: str, user_id: int):
    """URL'dan video/audio yuklash"""
    try:
        platform = get_platform(url)
        
        if platform == 'other':
            await message.answer(
                "❌ Faqat bu platformalar qo'llab-quvvatlanadi:\n"
                "🎬 YouTube\n"
                "📸 Instagram\n"
                "🎵 TikTok\n"
                "📘 Facebook"
            )
            return
        
        status = await message.answer("⏳ <b>Video yuklanmoqda...</b> ⏳")
        
        filename, full_title, duration = await download_video(url, user_id)
        
        try:
            await status.delete()
        except:
            pass
        
        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                
                if file_size > Config.MAX_FILE_SIZE:
                    await message.answer(
                        f"❌ Video juda katta!\n"
                        f"📦 Hajmi: {format_size(file_size)}\n"
                        f"📏 Maksimal: {format_size(Config.MAX_FILE_SIZE)}"
                    )
                    os.remove(filename)
                    return
                
                url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
                artist, title = extract_artist_title(full_title)
                
                identified_song = None
                if platform in ['instagram', 'tiktok', 'facebook']:
                    detect_msg = await message.answer("🎵 <b>Video ichidagi qo'shiq aniqlanmoqda...</b> 🔍")
                    identified_song = await identify_audio_from_video(filename)
                    try:
                        await detect_msg.delete()
                    except:
                        pass
                
                if identified_song:
                    search_title = identified_song['full_title']
                    search_artist = identified_song['artist']
                else:
                    search_title = full_title
                    search_artist = artist
                
                await video_cache.set(url_hash, {
                    'url': url,
                    'title': full_title,
                    'artist': search_artist,
                    'clean_title': title,
                    'duration': duration,
                    'platform': platform,
                    'identified_song': identified_song,
                    'search_query': search_title,
                })
                
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
                    [InlineKeyboardButton(text="🔍 Oxshashlar", callback_data=f"similar_{url_hash}")]
                ])
                
                platform_emoji = {
                    'youtube': '🎬',
                    'instagram': '📸',
                    'tiktok': '🎵',
                    'facebook': '📘'
                }
                
                video_file = FSInputFile(filename)
                
                caption = f"{platform_emoji.get(platform, '📹')} <b>{full_title[:50]}</b>\n⏱️ {format_duration(duration)}"
                if identified_song:
                    caption += f"\n\n🎯 <b>Aniqlangan qo'shiq:</b>\n{identified_song['full_title'][:80]}"
                caption += f"\n\n❤️ @zurnavolarbot"
                
                await message.answer_video(video_file, caption=caption, reply_markup=keyboard)
                os.remove(filename)
            except Exception as e:
                logger.error(f"Video jo'natishda xatolik: {e}")
                await message.answer("❌ Video jo'natishda xatolik!")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            error_msg = full_title if isinstance(full_title, str) and len(full_title) > 10 else "Nomalum xatolik"
            await message.answer(
                f"❌ Video yuklab bo'lmadi!\n\n"
                f"📝 <b>Sabab:</b> {error_msg[:100]}\n\n"
                "💡 Iltimos, havolani qayta urinib ko'ring yoki boshqa videoni tanlang."
            )
    except Exception as e:
        logger.error(f"URL processingda xatolik: {e}")
        await message.answer("❌ URL'ni qayta tekshiring!")

async def process_search(message: Message, query: str, user_id: int):
    """Qo'shiq qidirish"""
    try:
        if len(query) < 2:
            await message.answer("❌ Iltimos, kamida 2 ta harf kiriting!")
            return
        
        status = await message.answer(f"🔍 <b>Qidirilmoqda:</b> <code>{query}</code>...")
        songs = await search_songs(query, limit=10)
        
        try:
            await status.delete()
        except:
            pass
        
        if not songs:
            await message.answer(
                f"❌ <code>{query}</code> uchun hech narsa topilmadi!\n\n"
                "💡 Boshqa qo'shiq nomini urinib ko'ring."
            )
            return
        
        songs_text = ""
        for s in songs:
            if s['artist']:
                songs_text += f"<code>{s['number']}</code>. {s['artist']} — {s['title']}\n   ⏱️ {s['duration']}\n\n"
            else:
                songs_text += f"<code>{s['number']}</code>. {s['title']}\n   ⏱️ {s['duration']}\n\n"
        
        builder = InlineKeyboardBuilder()
        for song in songs:
            song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
            temp_data[song_id] = SongData(
                id=song_id,
                url=song['url'],
                title=song['full_title'],
                duration=song['duration'],
                artist=song['artist'],
                platform='youtube'
            )
            if song['artist']:
                btn_text = f"{song['number']}. {song['artist'][:25]} — {song['title'][:25]}"
            else:
                btn_text = f"{song['number']}. {song['title'][:40]}"
            builder.button(text=btn_text, callback_data=f"dl_{song_id}")
        
        builder.adjust(1)
        
        await message.answer(
            f"🎵 <b>Qidiruv natijasi:</b> <code>{query}</code>\n\n{songs_text}👇 <b>Yuklab olish uchun tanlang:</b>\n\n❤️ @zurnavolarbot",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Qidirishda xatolik: {e}")
        await message.answer("❌ Qidirishda xatolik yuz berdi!")

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    """Video'dan MP3 yuklash"""
    try:
        url_hash = call.data.replace("mp3_", "")
        video_info = await video_cache.get(url_hash)
        
        if not video_info:
            await call.answer("❌ Video ma'lumoti topilmadi!", show_alert=True)
            return
        
        await call.answer("⏳ MP3 yuklanmoqda...")
        
        display_title = video_info.get('identified_song', {}).get('full_title', video_info['title'])[:50]
        status = await call.message.answer(f"⏳ <b>MP3 tayyorlanmoqda:</b> {display_title}...")
        
        filename, title = await download_mp3(video_info['url'], call.from_user.id)
        
        try:
            await status.delete()
        except:
            pass
        
        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                artist, song_title = extract_artist_title(title)
                
                await call.message.answer_audio(
                    FSInputFile(filename),
                    caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @zurnavolarbot",
                    title=song_title[:64],
                    performer=artist[:64] if artist else "Zurnavolar"
                )
                os.remove(filename)
            except Exception as e:
                logger.error(f"Audio jo'natishda xatolik: {e}")
                await call.message.answer("❌ Audio jo'natishda xatolik!")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            error_msg = title if isinstance(title, str) and len(title) > 10 else "Nomalum xatolik"
            await call.message.answer(f"❌ MP3 yuklab bo'lmadi!\n\n📝 <b>Sabab:</b> {error_msg[:100]}")
    except Exception as e:
        logger.error(f"MP3 callback da xatolik: {e}")
        await call.answer("❌ Xatolik yuz berdi!", show_alert=True)

@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    """Oxshash qo'shiqlarni qidirish"""
    try:
        url_hash = call.data.replace("similar_", "")
        video_info = await video_cache.get(url_hash)
        
        if not video_info:
            await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
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
        
        status = await call.message.answer(f"🔍 <b>Oxshash qo'shiqlar qidirilmoqda:</b> {search_query[:60]}...")
        
        all_songs = []
        seen_urls = set()
        
        # Asosiy qidiruv
        if search_query:
            songs1 = await search_songs(search_query, limit=10)
            for s in songs1:
                if s['url'] not in seen_urls:
                    all_songs.append(s)
                    seen_urls.add(s['url'])
        
        # Cover va remix qidiruvi
        if song_title:
            for q in [f"{song_title} cover version", f"{song_title} remix"]:
                songs = await search_songs(q, limit=5)
                for s in songs:
                    if s['url'] not in seen_urls:
                        all_songs.append(s)
                        seen_urls.add(s['url'])
        
        # Artist qidiruvi
        if len(all_songs) < 5 and artist:
            songs = await search_songs(artist, limit=5)
            for s in songs:
                if s['url'] not in seen_urls:
                    all_songs.append(s)
                    seen_urls.add(s['url'])
        
        try:
            await status.delete()
        except:
            pass
        
        if not all_songs:
            await call.message.answer("❌ Oxshash qo'shiqlar topilmadi!")
            return
        
        display_songs = all_songs[:10]
        songs_text = ""
        for idx, s in enumerate(display_songs, 1):
            if s['artist']:
                songs_text += f"\n<code>{idx}</code>. {s['artist']} — {s['title'][:50]}  <code>{s['duration']}</code>"
            else:
                songs_text += f"\n<code>{idx}</code>. {s['title'][:50]}  <code>{s['duration']}</code>"
        
        builder = InlineKeyboardBuilder()
        for idx, song in enumerate(display_songs, 1):
            song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
            temp_data[song_id] = SongData(
                id=song_id,
                url=song['url'],
                title=song['full_title'],
                duration=song['duration'],
                artist=song['artist'],
                platform='youtube'
            )
            btn_text = f"{idx}"
            builder.button(text=btn_text, callback_data=f"dl_{song_id}")
        
        builder.adjust(5)
        
        await call.message.answer(
            f"🎵 <b>Oxshash qo'shiqlar:</b> {search_query[:50]}\n\n{songs_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n🔍 <b>Topildi:</b> {len(all_songs)} ta versiya\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n👇 <b>Raqamni bosing:</b>\n\n❤️ @zurnavolarbot",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Similar callback da xatolik: {e}")
        await call.answer("❌ Xatolik yuz berdi!", show_alert=True)

@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    """Tanlangan qo'shiqni yuklash"""
    try:
        song_id = call.data.replace("dl_", "")
        song_data = temp_data.get(song_id)
        
        if not song_data:
            await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
            return
        
        await call.answer("⏳ MP3 yuklanmoqda...")
        status = await call.message.answer(f"⏳ <b>MP3 tayyorlanmoqda:</b> {song_data.title[:40]}...")
        
        filename, title = await download_mp3(song_data.url, call.from_user.id)
        
        try:
            await status.delete()
        except:
            pass
        
        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                artist, song_title = extract_artist_title(title)
                
                await call.message.answer_audio(
                    FSInputFile(filename),
                    caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @zurnavolarbot",
                    title=song_title[:64],
                    performer=artist[:64] if artist else "Zurnavolar"
                )
                os.remove(filename)
                temp_data.pop(song_id, None)
            except Exception as e:
                logger.error(f"Audio jo'natishda xatolik: {e}")
                await call.message.answer("❌ Audio jo'natishda xatolik!")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            error_msg = title if isinstance(title, str) and len(title) > 10 else "Nomalum xatolik"
            await call.message.answer(f"❌ MP3 yuklab bo'lmadi!\n\n📝 <b>Sabab:</b> {error_msg[:100]}")
    except Exception as e:
        logger.error(f"Download callback da xatolik: {e}")
        await call.answer("❌ Xatolik yuz berdi!", show_alert=True)

@dp.errors()
async def errors_handler(event, exception):
    """Global error handler"""
    if "message is not modified" not in str(exception).lower():
        logger.error(f"Bot xatosi: {exception}")
    return True

# =================== KEEP-ALIVE SERVER ===================
async def keep_alive_server():
    """HTTP server - hosting platformalari uchun"""
    async def handle_client(reader, writer):
        try:
            await reader.read(8192)
            response_body = json.dumps({
                "status": "alive",
                "bot": "ZurnavolarBot",
                "uptime": str(int(time.time())),
                "timestamp": datetime.now().isoformat()
            })
            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n{response_body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception as e:
            logger.error(f"Keep-alive server xatosi: {e}")
        finally:
            writer.close()
    
    try:
        server = await asyncio.start_server(
            handle_client,
            '0.0.0.0',
            Config.KEEP_ALIVE_PORT,
            reuse_address=True
        )
        logger.info(f"🟢 Keep-Alive server: 0.0.0.0:{Config.KEEP_ALIVE_PORT}")
        async with server:
            await server.serve_forever()
    except Exception as e:
        logger.error(f"Keep-alive server ishga tushmadi: {e}")

async def self_ping():
    """Bot'ni o'zini ping qilish"""
    await asyncio.sleep(30)
    ping_url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    
    async with aiohttp.ClientSession() as session:
        while bot_running:
            try:
                async with session.get(ping_url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.debug(f"✅ Self-Ping OK: {datetime.now().strftime('%H:%M:%S')}")
            except Exception as e:
                logger.warning(f"Ping xatosi: {e}")
            await asyncio.sleep(Config.PING_INTERVAL)

# =================== MAIN ===================
async def main():
    """Main bot function"""
    global bot_running
    
    logger.info("=" * 60)
    logger.info("🎵 ZURNAVOLAR BOT ISHGA TUSHUVELYAPTI...")
    logger.info("=" * 60)
    
    try:
        bot_info = await bot.get_me()
        logger.info(f"✅ Bot: @{bot_info.username}")
        logger.info(f"✅ Nomi: {bot_info.first_name}")
        logger.info(f"🎤 Shazam: {'✅ Faol' if SHAZAM_AVAILABLE else '❌ O\'chirilgan'}")
        logger.info(f"🔧 FFmpeg: {'✅ Faol' if shutil.which('ffmpeg') else '❌ O\'chirilgan'}")
        logger.info(f"🍪 Cookie: {'✅ Faol' if os.path.exists(Config.COOKIES_PATH) else '❌ Yo\'q'}")
        if os.path.exists(Config.COOKIES_PATH):
            logger.info(f"   Hajmi: {format_size(os.path.getsize(Config.COOKIES_PATH))}")
        logger.info(f"📁 Downloads: {Config.DOWNLOADS_PATH}")
        logger.info(f"⚙️ Max Workers: {Config.MAX_WORKERS}")
        logger.info(f"🔌 Keep-Alive Port: {Config.KEEP_ALIVE_PORT}")
    except Exception as e:
        logger.error(f"Bot ma'lumoti olishda xatolik: {e}")
    
    logger.info("=" * 60)
    
    # Background tasks
    asyncio.create_task(keep_alive_server())
    asyncio.create_task(self_ping())
    asyncio.create_task(cleanup_old_files())
    
    while bot_running:
        try:
            logger.info("🚀 Bot polling ishga tushdi...")
            await dp.start_polling(
                bot,
                allowed_updates=['message', 'callback_query'],
                skip_updates=False
            )
        except Exception as e:
            logger.error(f"❌ Polling xatosi: {e}")
            if bot_running:
                logger.info("⏳ 10 soniya keyin qayta uriniladi...")
                await asyncio.sleep(10)

def signal_handler(sig, frame):
    """Graceful shutdown"""
    global bot_running
    logger.info("\n⏹️ Bot to'xtatilmoqda...")
    bot_running = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ Bot to'xtatildi!")
    except Exception as e:
        logger.error(f"Fatal xatolik: {e}")
        sys.exit(1)
