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

try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False

load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DOWNLOADS_PATH = Path("downloads")
    TEMP_PATH = Path("temp_audio")
    COOKIES_PATH = Path("cookies.txt")
    MAX_FILE_SIZE = 50 * 1024 * 1024
    KEEP_ALIVE_PORT = int(os.getenv("PORT", "8080"))
    DOWNLOAD_TIMEOUT = 90

if not Config.BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

@dataclass
class SongData:
    id: str
    url: str
    title: str
    duration: str = "0:00"
    artist: str = ""

Config.DOWNLOADS_PATH.mkdir(exist_ok=True)
Config.TEMP_PATH.mkdir(exist_ok=True)

session = AiohttpSession(timeout=120)
bot = Bot(token=Config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=2)

temp_data: Dict[str, SongData] = {}
video_cache: Dict[str, dict] = {}
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True

# =================== COOKIE TEKSHIRISH ===================
def get_cookie_file():
    cookie_paths = [
        Config.COOKIES_PATH,
        Path("/app/cookies.txt"),
        Path("cookies.txt"),
    ]
    for cp in cookie_paths:
        if cp and cp.exists():
            return str(cp)
    return None

COOKIE_FILE = get_cookie_file()
if COOKIE_FILE:
    print(f"✅ Cookie fayl topildi: {COOKIE_FILE}")
else:
    print("⚠️ Cookie fayl topilmadi - ba'zi videolar yuklanmasligi mumkin")

# =================== YORDAMCHI ===================
def get_platform(url: str) -> str:
    patterns = {
        'youtube': ['youtube.com', 'youtu.be'],
        'instagram': ['instagram.com'],
        'tiktok': ['tiktok.com'],
        'facebook': ['facebook.com', 'fb.watch']
    }
    for platform, domains in patterns.items():
        if any(d in url.lower() for d in domains):
            return platform
    return 'other'

def format_duration(seconds):
    if not seconds:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

def format_size(b):
    for u in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}TB"

def clean_title(full: str):
    if not full:
        return "", ""
    if ' - ' in full:
        a, t = full.split(' - ', 1)
    elif ' — ' in full:
        a, t = full.split(' — ', 1)
    else:
        a, t = "", full
    t = re.sub(r'\(.*?\)|\[.*?\]|Official.*|MV|Music Video|Lyrics|HD|4K|Cover|Remix|Video', '', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip()
    a = re.sub(r'\(.*?\)|\[.*?\]', '', a).strip()
    if len(t) > 50:
        t = t[:47] + "..."
    if len(a) > 30:
        a = a[:27] + "..."
    return (a[:30], t[:50]) if a else ("", t[:50])

# =================== YUKLASH (COOKIE BILAN) ===================
def get_ydl_opts(extra=None):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'geo_bypass': True,
        'retries': 5,
        'socket_timeout': 30,
        'noplaylist': True,
        'ignoreerrors': False,
    }
    if COOKIE_FILE:
        opts['cookiefile'] = COOKIE_FILE
    if extra:
        opts.update(extra)
    return opts

async def download_video(url: str, uid: int):
    def run():
        try:
            opts = get_ydl_opts({
                'outtmpl': str(Config.DOWNLOADS_PATH / f"v_{uid}_{int(time.time())}_%(title)s.%(ext)s"),
                'format': 'best[height<=480][ext=mp4]/best[ext=mp4]'
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fn = ydl.prepare_filename(info)
                if not os.path.exists(fn):
                    for ext in ['.mp4', '.webm']:
                        if os.path.exists(fn.rsplit('.',1)[0] + ext):
                            fn = fn.rsplit('.',1)[0] + ext
                            break
                return fn, info.get('title', 'Video'), info.get('duration', 0)
        except Exception as e:
            return None, str(e), 0
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def download_mp3(url: str, uid: int):
    def run():
        try:
            opts = get_ydl_opts({
                'outtmpl': str(Config.DOWNLOADS_PATH / f"a_{uid}_{int(time.time())}_%(title)s.%(ext)s"),
                'format': 'bestaudio/best',
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info).rsplit('.',1)[0] + ".mp3", info.get('title', 'Audio')
        except Exception as e:
            return None, str(e)
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def search_songs(q: str, limit: int = 10) -> List[dict]:
    def run():
        try:
            opts = get_ydl_opts({'extract_flat': True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
                songs = []
                for i, item in enumerate(info.get('entries', []), 1):
                    if item and item.get('id'):
                        a, t = clean_title(item.get('title', ''))
                        songs.append({
                            'n': i, 't': t, 'a': a,
                            'd': format_duration(item.get('duration', 0)),
                            'u': f"https://youtube.com/watch?v={item['id']}"
                        })
                return songs
        except Exception as e:
            print(f"Qidiruv xatosi: {e}")
            return []
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    if not SHAZAM_AVAILABLE or not shazam:
        return None
    try:
        audio_path = Config.TEMP_PATH / f"sample_{int(time.time())}.mp3"
        cmd = ['ffmpeg', '-i', video_path, '-ss', '5', '-t', '15', '-q:a', '0', '-map', 'a', str(audio_path), '-y']
        subprocess.run(cmd, capture_output=True, timeout=30)
        if not audio_path.exists():
            return None
        result = await shazam.recognize(str(audio_path))
        audio_path.unlink()
        if result and 'track' in result:
            track = result['track']
            return {
                'title': track.get('title', ''),
                'artist': track.get('subtitle', ''),
                'full_title': f"{track.get('subtitle', '')} - {track.get('title', '')}"
            }
        return None
    except:
        return None

# =================== HANDLERS ===================
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "🎵 <b>Zurnavolar Bot</b>\n\n"
        "📥 Link yuboring\n"
        "🔍 Qo'shiq nomi yozing\n\n"
        "@zurnavolarbot"
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "📖 <b>Yordam</b>\n\n"
        "🎯 YouTube/Instagram/TikTok/Facebook linki\n"
        "🔍 Qo'shiq nomi yozing\n"
        "🎵 MP3 sifat: 192kbps\n\n"
        "@zurnavolarbot"
    )

@dp.message(F.text)
async def handle(m: Message):
    t = m.text.strip()
    if re.match(r'^https?://', t):
        await process_url(m, t)
    elif len(t) >= 2:
        await process_search(m, t)
    else:
        await m.answer("❌ Kamida 2 harf yoki link")

async def process_url(m: Message, url: str):
    plat = get_platform(url)
    if plat == 'other':
        await m.answer("❌ Faqat YouTube | Instagram | TikTok | Facebook")
        return
    
    msg = await m.answer("⏳ Yuklanmoqda...")
    try:
        fn, title, dur = await asyncio.wait_for(download_video(url, m.from_user.id), timeout=Config.DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        await msg.delete()
        await m.answer("❌ Vaqt tugadi")
        return
    await msg.delete()
    
    if not fn or not os.path.exists(fn):
        await m.answer(f"❌ {title[:100]}")
        return
    
    if os.path.getsize(fn) > Config.MAX_FILE_SIZE:
        await m.answer("❌ Juda katta")
        os.remove(fn)
        return
    
    hid = hashlib.md5(url.encode()).hexdigest()[:8]
    a, t = clean_title(title)
    
    identified = None
    if plat in ['instagram', 'tiktok', 'facebook']:
        det = await m.answer("🎵 Aniqlanmoqda...")
        identified = await identify_audio_from_video(fn)
        await det.delete()
    
    if identified:
        search_q = identified['full_title']
        a = identified['artist']
    else:
        search_q = title
    
    video_cache[hid] = {'url': url, 'title': title, 'artist': a, 'search': search_q}
    
    emoji = {'youtube':'🎬', 'instagram':'📸', 'tiktok':'🎵', 'facebook':'📘'}
    cap = f"{emoji.get(plat, '📹')} <b>{t}</b>  {format_duration(dur)}"
    if identified:
        cap += f"\n🎯 {identified['full_title'][:40]}"
    cap += "\n\n❤️ @zurnavolarbot"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3_{hid}")],
        [InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"sim_{hid}")]
    ])
    
    try:
        await m.answer_video(FSInputFile(fn), caption=cap, reply_markup=kb)
    except:
        await m.answer_video(FSInputFile(fn), caption=cap[:200])
    os.remove(fn)

async def process_search(m: Message, q: str):
    msg = await m.answer(f"🔍 {q}")
    songs = await search_songs(q)
    await msg.delete()
    
    if not songs:
        await m.answer("❌ Topilmadi")
        return
    
    result = f"🔍 {q}\n\n"
    for s in songs:
        if s['a']:
            result += f"{s['n']}. {s['a']} - {s['t']}\n   {s['d']}\n\n"
        else:
            result += f"{s['n']}. {s['t']}\n   {s['d']}\n\n"
    
    builder = InlineKeyboardBuilder()
    for s in songs:
        sid = hashlib.md5(s['u'].encode()).hexdigest()[:8]
        temp_data[sid] = SongData(id=sid, url=s['u'], title=s['t'], duration=s['d'], artist=s['a'])
        builder.button(text=f"{s['n']}", callback_data=f"dl_{sid}")
    builder.adjust(5)
    
    await m.answer(
        f"{result}👇 <b>Raqamni bosing</b>\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

# =================== CALLBACKS ===================
@dp.callback_query(F.data.startswith("mp3_"))
async def get_mp3(call: CallbackQuery):
    info = video_cache.get(call.data.replace("mp3_", ""))
    if not info:
        await call.answer("❌", show_alert=True)
        return
    await call.answer("⏳")
    msg = await call.message.answer("⏳ MP3 tayyor...")
    try:
        fn, title = await asyncio.wait_for(download_mp3(info['url'], call.from_user.id), timeout=Config.DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        await msg.delete()
        await call.message.answer("❌ Vaqt tugadi")
        return
    await msg.delete()
    if fn and os.path.exists(fn):
        a, t = clean_title(title)
        await call.message.answer_audio(
            FSInputFile(fn),
            caption=f"🎵 {t}\n📦 {format_size(os.path.getsize(fn))}\n\n❤️ @zurnavolarbot",
            title=t[:60],
            performer=a or "Zurnavolar"
        )
        os.remove(fn)
    else:
        await call.message.answer(f"❌ {title[:100]}")

@dp.callback_query(F.data.startswith("sim_"))
async def similar(call: CallbackQuery):
    info = video_cache.get(call.data.replace("sim_", ""))
    if not info:
        await call.answer("❌", show_alert=True)
        return
    await call.answer("🔍")
    msg = await call.message.answer(f"🔍 {info['search'][:35]}...")
    songs = await search_songs(info['search'], limit=10)
    await msg.delete()
    if not songs:
        await call.message.answer("❌ Oxshash topilmadi")
        return
    
    result = f"🔍 {info['search'][:35]}\n\n"
    for i, s in enumerate(songs[:10], 1):
        if s['a']:
            result += f"{i}. {s['a']} - {s['t']}\n   {s['d']}\n\n"
        else:
            result += f"{i}. {s['t']}\n   {s['d']}\n\n"
    
    builder = InlineKeyboardBuilder()
    for i, s in enumerate(songs[:10], 1):
        sid = hashlib.md5(s['u'].encode()).hexdigest()[:8]
        temp_data[sid] = SongData(id=sid, url=s['u'], title=s['t'], duration=s['d'], artist=s['a'])
        builder.button(text=f"{i}", callback_data=f"dl_{sid}")
    builder.adjust(5)
    
    await call.message.answer(
        f"{result}━━━━━━━━━━━━━━━━\n🔍 {len(songs)} ta versiya\n━━━━━━━━━━━━━━━━\n👇 Raqamni bosing\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("dl_"))
async def download(call: CallbackQuery):
    song = temp_data.get(call.data.replace("dl_", ""))
    if not song:
        await call.answer("❌", show_alert=True)
        return
    await call.answer("⏳")
    msg = await call.message.answer(f"⏳ {song.title[:30]}...")
    try:
        fn, title = await asyncio.wait_for(download_mp3(song.url, call.from_user.id), timeout=Config.DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        await msg.delete()
        await call.message.answer("❌ Vaqt tugadi")
        return
    await msg.delete()
    if fn and os.path.exists(fn):
        a, t = clean_title(title)
        await call.message.answer_audio(
            FSInputFile(fn),
            caption=f"🎵 {t}\n📦 {format_size(os.path.getsize(fn))}\n\n❤️ @zurnavolarbot",
            title=t[:60],
            performer=a or "Zurnavolar"
        )
        os.remove(fn)
        temp_data.pop(call.data.replace("dl_", ""), None)
    else:
        await call.message.answer(f"❌ {title[:100]}")

@dp.errors()
async def err(e, ex):
    logging.error(f"Xato: {ex}")
    return True

# =================== KEEP-ALIVE ===================
async def keep_alive():
    async def h(r, w):
        try:
            await r.read(100)
            w.write(b"HTTP/1.1 200 OK\r\n\r\nOK")
            await w.drain()
        except:
            pass
        finally:
            w.close()
    server = await asyncio.start_server(h, '0.0.0.0', Config.KEEP_ALIVE_PORT, reuse_address=True)
    print(f"🟢 Keep-Alive: {Config.KEEP_ALIVE_PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    async with aiohttp.ClientSession() as s:
        while bot_running:
            try:
                await s.get(f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}", timeout=5)
                print(f"✅ Ping: {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(300)

# =================== MAIN ===================
async def main():
    global bot_running
    logging.basicConfig(level=logging.INFO)
    
    # Webhook tozalash
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook tozalandi")
    except Exception as e:
        print(f"⚠️ Webhook xatosi: {e}")
    
    try:
        me = await bot.get_me()
        print("=" * 35)
        print(f"🎵 Zurnavolar: @{me.username}")
        print(f"🍪 Cookie: {'✅' if COOKIE_FILE else '❌'}")
        print(f"🎬 FFmpeg: {'✅' if shutil.which('ffmpeg') else '❌'}")
        print("=" * 35)
    except:
        pass
    
    asyncio.create_task(keep_alive())
    asyncio.create_task(self_ping())
    
    while bot_running:
        try:
            print("🚀 Bot ishga tushdi")
            await dp.start_polling(bot, allowed_updates=['message', 'callback_query'], skip_updates=True)
        except Exception as e:
            print(f"❌ {e} - 5s")
            await asyncio.sleep(5)

def signal_handler(sig, frame):
    global bot_running
    bot_running = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ To'xtatildi!")
