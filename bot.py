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

try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

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
    raise ValueError("❌ BOT_TOKEN topilmadi!")

def check_dependencies():
    if not shutil.which('ffmpeg'):
        logger.warning("⚠️ ffmpeg o'rnatilmagan")
        return False
    logger.info("✅ Dependencies OK")
    return True

check_dependencies()

class ExpiringCache:
    def __init__(self, max_age=3600):
        self.cache = OrderedDict()
        self.max_age = max_age
        self.lock = asyncio.Lock()

    async def set(self, key, value):
        async with self.lock:
            self.cache[key] = {'data': value, 'timestamp': datetime.now()}
            await self._cleanup()

    async def get(self, key):
        async with self.lock:
            if key in self.cache:
                item = self.cache[key]
                if datetime.now() - item['timestamp'] < timedelta(seconds=self.max_age):
                    return item['data']
                del self.cache[key]
            return None

    async def _cleanup(self):
        expired = [k for k, v in self.cache.items()
                   if datetime.now() - v['timestamp'] > timedelta(seconds=self.max_age)]
        for k in expired:
            del self.cache[k]

    async def size(self):
        async with self.lock:
            return len(self.cache)

def create_cookies_files():
    if Config.COOKIES_CONTENT:
        try:
            os.makedirs(os.path.dirname(Config.COOKIES_PATH) or '.', exist_ok=True)
            with open(Config.COOKIES_PATH, 'w', encoding='utf-8') as f:
                f.write(Config.COOKIES_CONTENT)
            logger.info(f"✅ YouTube cookie: {Config.COOKIES_PATH}")
        except Exception as e:
            logger.error(f"YouTube cookie xatosi: {e}")

    if Config.INSTAGRAM_COOKIES_CONTENT:
        try:
            os.makedirs(os.path.dirname(Config.INSTAGRAM_COOKIES_PATH) or '.', exist_ok=True)
            with open(Config.INSTAGRAM_COOKIES_PATH, 'w', encoding='utf-8') as f:
                f.write(Config.INSTAGRAM_COOKIES_CONTENT)
            logger.info(f"✅ Instagram cookie: {Config.INSTAGRAM_COOKIES_PATH}")
        except Exception as e:
            logger.error(f"Instagram cookie xatosi: {e}")

@dataclass
class SongData:
    id: str
    url: str
    title: str
    duration: str = "0:00"
    artist: str = ""
    platform: str = 'youtube'
    duration_seconds: int = 0

Config.DOWNLOADS_PATH.mkdir(exist_ok=True, parents=True)
Config.TEMP_PATH.mkdir(exist_ok=True, parents=True)
create_cookies_files()

session = AiohttpSession(timeout=Config.SESSION_TIMEOUT)
bot = Bot(token=Config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS, thread_name_prefix="w")

temp_data: Dict[str, SongData] = {}
video_cache = ExpiringCache(max_age=Config.CACHE_EXPIRY)
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True

def get_platform(url: str) -> str:
    url_lower = url.lower()
    patterns = {
        'youtube': ['youtube.com', 'youtu.be'],
        'instagram': ['instagram.com', 'instagr.am'],
        'tiktok': ['tiktok.com', 'vm.tiktok.com'],
        'facebook': ['facebook.com', 'fb.watch'],
    }
    for platform, domains in patterns.items():
        if any(domain in url_lower for domain in domains):
            return platform
    return 'other'

def format_duration(seconds):
    if not seconds:
        return "0:00"
    try:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
    except:
        return "0:00"

def format_size(b):
    if not b:
        return "0 B"
    for u in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def extract_artist_title(full_title: str):
    if not full_title:
        return "", ""
    if ' - ' in full_title:
        artist, title = full_title.split(' - ', 1)
    elif ' — ' in full_title:
        artist, title = full_title.split(' — ', 1)
    else:
        return "", full_title.strip()

    clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title)
    remove_words = ['Official Video', 'Official Music Video', 'Official Audio',
                    'MV', 'M/V', 'Music Video', 'Lyrics', 'HD', '4K', '1080p',
                    'TikTok', 'Trend', 'Viral', 'Cover', 'AI Cover', 'Remix', 'Extended']
    for w in remove_words:
        clean_title = re.sub(re.escape(w), '', clean_title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
    return clean_artist.strip(), clean_title

def get_cookies_for_platform(platform='youtube'):
    opts = {}
    if platform == 'instagram' and os.path.exists(Config.INSTAGRAM_COOKIES_PATH):
        opts['cookiefile'] = Config.INSTAGRAM_COOKIES_PATH
    elif os.path.exists(Config.COOKIES_PATH):
        opts['cookiefile'] = Config.COOKIES_PATH
    return opts

def get_ydl_opts(output_path, format_type='video', platform='youtube'):
    opts = {
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'retries': 5,
        'fragment_retries': 5,
        'retry_sleep': 3,
        'socket_timeout': Config.SOCKET_TIMEOUT,
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    }
    if shutil.which('node'):
        opts['js_runtimes'] = {'node': {}}
        opts['remote_components'] = ['ejs:github']
    if format_type == 'video':
        opts.update({'format': 'best[height<=720][ext=mp4]/best[ext=mp4]', 'merge_output_format': 'mp4'})
    elif format_type == 'audio':
        opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]})
    opts.update(get_cookies_for_platform(platform))
    return opts

async def identify_audio_from_video(video_path):
    if not SHAZAM_AVAILABLE or not shazam or not os.path.exists(video_path):
        return None
    try:
        audio_path = video_path.replace('.mp4', '_s.mp3')
        cmd = ['ffmpeg', '-i', video_path, '-ss', '5', '-t', str(Config.AUDIO_SAMPLE_DURATION),
               '-q:a', '0', '-map', 'a', audio_path, '-y', '-loglevel', 'quiet']
        result = await asyncio.get_event_loop().run_in_executor(pool, lambda: subprocess.run(cmd, capture_output=True, text=True))
        if result.returncode != 0 or not os.path.exists(audio_path):
            return None
        try:
            res = await asyncio.wait_for(shazam.recognize(audio_path), timeout=20)
        except:
            res = None
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)
        if res and 'track' in res:
            t = res['track']
            return {'title': t.get('title', ''), 'artist': t.get('subtitle', ''),
                    'full_title': f"{t.get('subtitle', '')} - {t.get('title', '')}"}
        return None
    except:
        return None

async def download_video(url, user_id):
    def run():
        try:
            platform = get_platform(url)
            output_path = str(Config.DOWNLOADS_PATH / f"v_{user_id}_{int(time.time())}_%(title)s.%(ext)s")
            opts = get_ydl_opts(output_path, 'video', platform)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    base = filename.rsplit('.', 1)[0]
                    for ext in ['.mp4', '.webm', '.mkv', '.mov']:
                        test = base + ext
                        if os.path.exists(test) and os.path.getsize(test) > 0:
                            filename = test
                            break
                if os.path.exists(filename) and os.path.getsize(filename) == 0:
                    os.remove(filename)
                    return None, "Fayl bo'sh", 0
                return filename, info.get('title', 'Video'), info.get('duration', 0)
        except Exception as e:
            return None, str(e), 0
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def download_mp3(url, user_id):
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
            output_path = str(Config.DOWNLOADS_PATH / f"a_{user_id}_{int(time.time())}_%(title)s.%(ext)s")
            opts = get_ydl_opts(output_path, 'audio', platform)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                if os.path.exists(filename) and os.path.getsize(filename) == 0:
                    os.remove(filename)
                    return None, "Fayl bo'sh"
                return filename, info.get('title', 'Audio')
        except Exception as e:
            return None, str(e)
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def search_songs(query, limit=10):
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
                            ft = item.get('title', '')
                            a, t = extract_artist_title(ft)
                            d = item.get('duration', 0) or 0
                            songs.append({'number': i, 'title': t[:60], 'artist': a[:40],
                                          'full_title': ft[:80], 'duration': format_duration(d),
                                          'duration_seconds': d, 'url': f"https://youtube.com/watch?v={item['id']}"})
                return songs
        except:
            return []
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def cleanup_old_files():
    while bot_running:
        try:
            await asyncio.sleep(1800)
            for d in [Config.DOWNLOADS_PATH, Config.TEMP_PATH]:
                if d.exists():
                    for f in d.glob('*'):
                        if f.is_file() and time.time() - f.stat().st_mtime > 1800:
                            try:
                                f.unlink()
                            except:
                                pass
        except:
            pass

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"🎵 <b>Zurnavolar Bot</b>\n\n"
        "📥 Link: YouTube | Instagram | TikTok | Facebook\n"
        "🔍 Qidirish: <code>shoxruxon</code>\n"
        "🎯 Instagram/TikTok video = avtoaniqlash\n"
        "⚠️ MP3 ≤ 10 daqiqa\n\n/help | /about\n❤️ @zurnavolarbot"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 1️⃣ Link yuboring 2️⃣ Nom yozing 3️⃣ Video yuboring\n\n"
        "✅ MP3 192kbps | Video 720p | Shazam\n"
        f"⚠️ MP3 ≤10min | Fayl ≤{format_size(Config.MAX_FILE_SIZE)}\n❤️ @zurnavolarbot"
    )

@dp.message(Command("about"))
async def cmd_about(message: Message):
    try:
        bi = await bot.get_me()
        cs = await video_cache.size()
        await message.answer(
            f"ℹ️ {bi.first_name} | @{bi.username}\n"
            f"🛠️ Shazam:{'✅' if SHAZAM_AVAILABLE else '❌'} FFmpeg:{'✅' if shutil.which('ffmpeg') else '❌'}\n"
            f"🍪 YT:{'✅' if os.path.exists(Config.COOKIES_PATH) else '❌'} IG:{'✅' if os.path.exists(Config.INSTAGRAM_COOKIES_PATH) else '❌'}\n"
            f"📊 Cache:{cs} | MP3≤10min\n❤️ @zurnavolarbot"
        )
    except:
        pass

@dp.message(F.text)
async def handle_message(message: Message):
    try:
        text = message.text.strip()
        if re.match(r'^https?://', text):
            await process_url(message, text, message.from_user.id)
        else:
            await process_search(message, text, message.from_user.id)
    except:
        await message.answer("❌ Xatolik!")

async def process_url(message, url, user_id):
    try:
        platform = get_platform(url)
        if platform == 'other':
            await message.answer("❌ YouTube | Instagram | TikTok | Facebook")
            return

        status = await message.answer("⏳ Yuklanmoqda...")
        filename, full_title, duration = await download_video(url, user_id)
        await status.delete()

        if filename and os.path.exists(filename):
            try:
                file_size = os.path.getsize(filename)
                if file_size > Config.MAX_FILE_SIZE:
                    await message.answer(f"❌ Juda katta! {format_size(file_size)}")
                    os.remove(filename)
                    return

                url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
                artist, title = extract_artist_title(full_title)

                identified_song = None
                if platform in ['instagram', 'tiktok', 'facebook']:
                    identified_song = await identify_audio_from_video(filename)

                st = identified_song['full_title'] if identified_song else full_title
                sa = identified_song['artist'] if identified_song else artist

                await video_cache.set(url_hash, {
                    'url': url, 'title': full_title, 'artist': sa,
                    'clean_title': title, 'duration': duration, 'platform': platform,
                    'identified_song': identified_song, 'search_query': st
                })

                if duration <= Config.MAX_AUDIO_DURATION:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3_{url_hash}"),
                         InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"similar_{url_hash}")]
                    ])
                else:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"similar_{url_hash}")]
                    ])

                emoji = {'youtube': '🎬', 'instagram': '📸', 'tiktok': '🎵', 'facebook': '📘'}
                vf = FSInputFile(filename)
                cap = f"{emoji.get(platform, '📹')} <b>{full_title[:50]}</b>\n⏱️ {format_duration(duration)}"
                if identified_song:
                    cap += f"\n🎯 {identified_song['full_title'][:80]}"
                if duration > Config.MAX_AUDIO_DURATION:
                    m, s = divmod(duration, 60)
                    cap += f"\n⚠️ MP3 yo'q ({m}:{s:02d})"
                cap += f"\n\n❤️ @zurnavolarbot"

                try:
                    await asyncio.wait_for(
                        message.answer_video(vf, caption=cap, reply_markup=kb),
                        timeout=Config.UPLOAD_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    await message.answer(f"⚠️ Timeout! Hajm: {format_size(file_size)}")
                finally:
                    if os.path.exists(filename):
                        os.remove(filename)
            except Exception as e:
                logger.error(f"Video jo'natish: {e}")
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            err = str(full_title)[:200] if full_title else "Nomalum"
            if "login required" in err.lower():
                await message.answer("❌ Instagram login kerak!")
            elif "bo'sh" in err.lower():
                await message.answer("❌ Yuklangan fayl bo'sh! Qayta urinib ko'ring.")
            else:
                await message.answer(f"❌ {err[:150]}")
    except Exception as e:
        logger.error(f"URL: {e}")

async def process_search(message, query, user_id):
    try:
        if len(query) < 2:
            await message.answer("❌ Kamida 2 ta harf!")
            return

        status = await message.answer(f"🔍 <b>{query}</b>...")
        songs = await search_songs(query)
        await status.delete()

        if not songs:
            await message.answer(f"❌ <code>{query}</code> topilmadi!")
            return

        txt = ""
        for s in songs:
            dur = f"🔴{s['duration']}" if s.get('duration_seconds', 0) > Config.MAX_AUDIO_DURATION else s['duration']
            txt += f"<code>{s['number']:>2}</code> {s['artist']} — {s['title']}  <code>{dur}</code>\n"

        builder = InlineKeyboardBuilder()
        for song in songs:
            sid = hashlib.md5(song['url'].encode()).hexdigest()[:10]
            temp_data[sid] = SongData(id=sid, url=song['url'], title=song['full_title'],
                                      duration=song['duration'], artist=song['artist'],
                                      platform='youtube', duration_seconds=song.get('duration_seconds', 0))
            builder.button(text=f"{song['number']}", callback_data=f"dl_{sid}")

        builder.adjust(5)
        await message.answer(
            f"🎵 <b>{query}</b>\n\n{txt}\n👇 Raqamni bosing | 🔴 10min+\n❤️ @zurnavolarbot",
            reply_markup=builder.as_markup()
        )
    except:
        await message.answer("❌ Qidirishda xatolik!")

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    try:
        url_hash = call.data.replace("mp3_", "")
        vi = await video_cache.get(url_hash)
        if not vi:
            await call.answer("❌ Topilmadi!", show_alert=True)
            return

        d = vi.get('duration', 0)
        if d > Config.MAX_AUDIO_DURATION:
            m, s = divmod(d, 60)
            await call.answer(f"❌ {m}:{s:02d}", show_alert=True)
            return

        await call.answer("⏳ MP3...")
        disp = vi.get('identified_song', {}).get('full_title', vi['title'])[:50]
        status = await call.message.answer(f"⏳ <b>{disp}</b>...")
        filename, title = await download_mp3(vi['url'], call.from_user.id)
        await status.delete()

        if filename and os.path.exists(filename):
            try:
                sz = os.path.getsize(filename)
                a, st = extract_artist_title(title)
                await call.message.answer_audio(
                    FSInputFile(filename),
                    caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(sz)}\n❤️ @zurnavolarbot",
                    title=st[:64], performer=a[:64] if a else "Zurnavolar"
                )
                os.remove(filename)
            except:
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            err = str(title)
            if "VIDEO_JUDA_UZUN" in err:
                dur = int(err.split(":")[1])
                m, s = divmod(dur, 60)
                await call.message.answer(f"❌ {m}:{s:02d}")
            elif "bo'sh" in err.lower():
                await call.message.answer("❌ Fayl bo'sh! Qayta urinib ko'ring.")
            else:
                await call.message.answer(f"❌ {err[:100]}")
    except:
        await call.answer("❌ Xatolik!", show_alert=True)

@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    try:
        url_hash = call.data.replace("similar_", "")
        vi = await video_cache.get(url_hash)
        if not vi:
            await call.answer("❌ Topilmadi!", show_alert=True)
            return

        await call.answer("🔍...")
        if vi.get('identified_song'):
            sq = vi['identified_song']['full_title']
            art = vi['identified_song'].get('artist', '')
            sti = vi['identified_song'].get('title', '')
        else:
            art = vi.get('artist', '')
            sti = vi.get('clean_title', '')
            sq = f"{art} {sti}".strip()

        status = await call.message.answer(f"🔍 <b>{sq[:60]}</b>...")
        all_songs, seen = [], set()

        if sq:
            for s in await search_songs(sq, 10):
                if s['url'] not in seen:
                    all_songs.append(s)
                    seen.add(s['url'])
        if sti:
            for q in [f"{sti} cover version", f"{sti} remix"]:
                for s in await search_songs(q, 5):
                    if s['url'] not in seen:
                        all_songs.append(s)
                        seen.add(s['url'])
        if len(all_songs) < 5 and art:
            for s in await search_songs(art, 5):
                if s['url'] not in seen:
                    all_songs.append(s)
                    seen.add(s['url'])

        await status.delete()

        if not all_songs:
            await call.message.answer("❌ Topilmadi!")
            return

        ds = all_songs[:10]
        txt = ""
        for idx, s in enumerate(ds, 1):
            dur = f"🔴{s['duration']}" if s.get('duration_seconds', 0) > Config.MAX_AUDIO_DURATION else s['duration']
            txt += f"<code>{idx:>2}</code> {s['artist']} — {s['title'][:50]}  <code>{dur}</code>\n"

        builder = InlineKeyboardBuilder()
        for idx, song in enumerate(ds, 1):
            sid = hashlib.md5(song['url'].encode()).hexdigest()[:10]
            temp_data[sid] = SongData(id=sid, url=song['url'], title=song['full_title'],
                                      duration=song['duration'], artist=song['artist'],
                                      platform='youtube', duration_seconds=song.get('duration_seconds', 0))
            builder.button(text=f"{idx}", callback_data=f"dl_{sid}")

        builder.adjust(5)
        await call.message.answer(
            f"🎵 <b>{sq[:50]}</b>\n\n{txt}\n━━━ 🔍{len(all_songs)}ta | 🔴10min+ ━━━\n👇 Raqam\n❤️ @zurnavolarbot",
            reply_markup=builder.as_markup()
        )
    except:
        await call.answer("❌ Xatolik!", show_alert=True)

@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    try:
        song_id = call.data.replace("dl_", "")
        sd = temp_data.get(song_id)
        if not sd:
            await call.answer("❌ Topilmadi!", show_alert=True)
            return

        if sd.duration_seconds > Config.MAX_AUDIO_DURATION:
            m, s = divmod(sd.duration_seconds, 60)
            await call.answer(f"❌ {m}:{s:02d}", show_alert=True)
            return

        await call.answer("⏳ MP3...")
        status = await call.message.answer(f"⏳ <b>{sd.title[:40]}</b>...")
        filename, title = await download_mp3(sd.url, call.from_user.id)
        await status.delete()

        if filename and os.path.exists(filename):
            try:
                sz = os.path.getsize(filename)
                a, st = extract_artist_title(title)
                await call.message.answer_audio(
                    FSInputFile(filename),
                    caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(sz)}\n❤️ @zurnavolarbot",
                    title=st[:64], performer=a[:64] if a else "Zurnavolar"
                )
                os.remove(filename)
                temp_data.pop(song_id, None)
            except:
                try:
                    os.remove(filename)
                except:
                    pass
        else:
            err = str(title)
            if "VIDEO_JUDA_UZUN" in err:
                dur = int(err.split(":")[1])
                m, s = divmod(dur, 60)
                await call.message.answer(f"❌ {m}:{s:02d}")
            elif "bo'sh" in err.lower():
                await call.message.answer("❌ Fayl bo'sh! Qayta urinib ko'ring.")
            else:
                await call.message.answer(f"❌ {err[:100]}")
    except:
        await call.answer("❌ Xatolik!", show_alert=True)

@dp.errors()
async def errors_handler(event, exception):
    if "message is not modified" not in str(exception).lower():
        logger.error(f"Bot xatosi: {exception}")
    return True

async def keep_alive_server():
    async def handle_client(reader, writer):
        try:
            await reader.read(8192)
            body = json.dumps({"status": "alive", "bot": "ZurnavolarBot",
                               "uptime": str(int(time.time())),
                               "timestamp": datetime.now().isoformat()})
            resp = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
            writer.write(resp.encode())
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
        logger.error(f"Server: {e}")

async def self_ping():
    await asyncio.sleep(30)
    url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    async with aiohttp.ClientSession() as s:
        while bot_running:
            try:
                async with s.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.debug(f"✅ Ping: {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(Config.PING_INTERVAL)

async def main():
    global bot_running
    logger.info("=" * 50)
    logger.info("🎵 ZURNAVOLAR BOT")
    logger.info("=" * 50)

    try:
        bi = await bot.get_me()
        logger.info(f"✅ @{bi.username} | {bi.first_name}")
        logger.info(f"🎤 Shazam: {'✅' if SHAZAM_AVAILABLE else '❌'} | FFmpeg: {'✅' if shutil.which('ffmpeg') else '❌'}")
        logger.info(f"🟢 Node: {'✅' if shutil.which('node') else '❌'}")
        logger.info(f"🍪 YT: {'✅' if os.path.exists(Config.COOKIES_PATH) else '❌'} | IG: {'✅' if os.path.exists(Config.INSTAGRAM_COOKIES_PATH) else '❌'}")
        logger.info(f"⏱️ MP3≤10min | ⏫ {Config.UPLOAD_TIMEOUT}s")
    except:
        pass

    logger.info("=" * 50)

    asyncio.create_task(keep_alive_server())
    asyncio.create_task(self_ping())
    asyncio.create_task(cleanup_old_files())

    while bot_running:
        try:
            logger.info("🚀 Polling...")
            await dp.start_polling(bot, allowed_updates=['message', 'callback_query'], skip_updates=False)
        except Exception as e:
            logger.error(f"❌ {e}")
            if bot_running:
                await asyncio.sleep(10)

def signal_handler(sig, frame):
    global bot_running
    logger.info("⏹️ To'xtatilmoqda...")
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
        logger.error(f"Fatal: {e}")
        sys.exit(1)
