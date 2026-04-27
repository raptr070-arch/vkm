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
    DOWNLOAD_TIMEOUT = 60

if not Config.BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

# =================== DATA MODELS (ISHLATISHDAN OLDIN E'LON) ===================
@dataclass
class SongData:
    id: str
    url: str
    title: str
    duration: str = "0:00"
    artist: str = ""
    platform: str = 'youtube'

# =================== INIT ===================
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

# =================== YORDAMCHI ===================
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
    
    # Tozalash
    clean_title = re.sub(r'\(.*?\)|\[.*?\]|Official.*|MV|Music Video|Lyrics|HD|4K|Cover|Remix', '', title, flags=re.IGNORECASE)
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
    
    if not clean_title or len(clean_title) < 3:
        clean_title = title.strip()
    
    return clean_artist[:30], clean_title[:50]

# =================== YUKLASH FUNKSIYALARI ===================
def get_ydl_opts():
    return {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,
        'geo_bypass': True,
        'retries': 3,
        'socket_timeout': 30,
        'noplaylist': True,
    }

async def download_video(url: str, user_id: int):
    def run():
        try:
            opts = get_ydl_opts()
            opts.update({
                'outtmpl': str(Config.DOWNLOADS_PATH / f"v_{user_id}_{int(time.time())}_%(title)s.%(ext)s"),
                'format': 'best[height<=480][ext=mp4]/best[ext=mp4]',
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    base = filename.rsplit('.', 1)[0]
                    for ext in ['.mp4', '.webm']:
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
            opts = get_ydl_opts()
            opts.update({
                'outtmpl': str(Config.DOWNLOADS_PATH / f"a_{user_id}_{int(time.time())}_%(title)s.%(ext)s"),
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
                return filename, info.get('title', 'Audio')
        except Exception as e:
            return None, str(e)
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def search_songs(query: str, limit: int = 8) -> List[dict]:
    def run():
        try:
            opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'ignoreerrors': True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                songs = []
                for i, item in enumerate(info.get('entries', []), 1):
                    if item and item.get('id'):
                        full = item.get('title', 'Noma\'lum')
                        artist, title = extract_artist_title(full)
                        songs.append({
                            'num': i,
                            'title': title,
                            'artist': artist,
                            'duration': format_duration(item.get('duration', 0)),
                            'url': f"https://youtube.com/watch?v={item['id']}",
                        })
                return songs
        except:
            return []
    return await asyncio.get_event_loop().run_in_executor(pool, run)

async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    if not SHAZAM_AVAILABLE or not shazam:
        return None
    try:
        audio_path = video_path.replace('.mp4', '_sample.mp3')
        subprocess.run(['ffmpeg', '-i', video_path, '-ss', '5', '-t', '15', '-q:a', '0', '-map', 'a', audio_path, '-y'],
                      capture_output=True, timeout=30)
        if not os.path.exists(audio_path):
            return None
        result = await shazam.recognize(audio_path)
        os.remove(audio_path)
        if result and 'track' in result:
            track = result['track']
            return {'title': track.get('title', ''), 'artist': track.get('subtitle', ''),
                   'full_title': f"{track.get('subtitle', '')} - {track.get('title', '')}"}
        return None
    except:
        return None

# =================== HANDLERS ===================
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎵 <b>Zurnavolar Bot</b> 🎵\n\n"
        "▫️ YouTube/Instagram/TikTok <b>link</b> yuboring\n"
        "▫️ Qo'shiq <b>nomini</b> yozib qidiring\n"
        "▫️ Instagram videodan <b>avto aniqlash</b>\n\n"
        "💬 @zurnavolarbot"
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "📖 <b>Yordam</b>\n\n"
        "🎯 <b>Link yuboring:</b> YouTube, Instagram, TikTok, Facebook\n"
        "🔍 <b>Qo'shiq nomi:</b> \"Jaloliddin Ahmadali\" yozing\n"
        "🎵 <b>MP3 sifat:</b> 192 kbps\n"
        "📹 <b>Video sifat:</b> 480p\n\n"
        "✨ @zurnavolarbot"
    )

@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    if re.match(r'^https?://', text):
        await process_url(message, text)
    elif len(text) >= 2:
        await process_search(message, text)
    else:
        await message.answer("❌ Kamida 2 harf yoki link yuboring")

async def process_url(message: Message, url: str):
    platform = get_platform(url)
    if platform == 'other':
        await message.answer("❌ Faqat YouTube | Instagram | TikTok | Facebook")
        return
    
    msg = await message.answer("⏳ Yuklanmoqda...")
    
    try:
        filename, title, duration = await asyncio.wait_for(download_video(url, message.from_user.id), timeout=60)
    except:
        await msg.delete()
        await message.answer("❌ Yuklash vaqti tugadi")
        return
    
    await msg.delete()
    
    if not filename or not os.path.exists(filename):
        await message.answer(f"❌ {title[:100]}")
        return
    
    size = os.path.getsize(filename)
    if size > Config.MAX_FILE_SIZE:
        await message.answer(f"❌ Juda katta: {format_size(size)}")
        os.remove(filename)
        return
    
    hid = hashlib.md5(url.encode()).hexdigest()[:8]
    artist, clean_title = extract_artist_title(title)
    
    # Audio aniqlash
    identified = None
    if platform in ['instagram', 'tiktok', 'facebook']:
        det = await message.answer("🎵 Aniqlanmoqda...")
        identified = await identify_audio_from_video(filename)
        await det.delete()
    
    if identified:
        search_q = identified['full_title']
        artist = identified['artist']
    else:
        search_q = title
    
    video_cache[hid] = {'url': url, 'title': title, 'artist': artist, 'search': search_q, 'identified': identified}
    
    # Ixcham caption
    plat_emoji = {'youtube':'🎬', 'instagram':'📸', 'tiktok':'🎵', 'facebook':'📘'}
    cap = f"{plat_emoji.get(platform, '📹')} <b>{clean_title[:45]}</b>  ⏱{format_duration(duration)}"
    if identified:
        cap += f"\n🎯 {identified['full_title'][:40]}"
    cap += "\n\n❤️ @zurnavolarbot"
    
    # TUGMALAR
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3_{hid}")],
        [InlineKeyboardButton(text="🔍 Oxshash", callback_data=f"sim_{hid}")]
    ])
    
    try:
        await message.answer_video(FSInputFile(filename), caption=cap, reply_markup=keyboard)
    except:
        await message.answer_video(FSInputFile(filename), caption=cap[:200])
    
    os.remove(filename)

async def process_search(message: Message, query: str):
    msg = await message.answer(f"🔍 <i>{query}</i>")
    songs = await search_songs(query)
    await msg.delete()
    
    if not songs:
        await message.answer("❌ Topilmadi")
        return
    
    # Ixcham ko'rinish
    result = f"🎵 <b>{query}</b>\n\n"
    for s in songs:
        if s['artist']:
            result += f"{s['num']}. {s['artist']} — {s['title']}\n   ⏱ {s['duration']}\n\n"
        else:
            result += f"{s['num']}. {s['title']}\n   ⏱ {s['duration']}\n\n"
    
    # TUGMALAR FAQAT RAQAM
    builder = InlineKeyboardBuilder()
    for s in songs:
        sid = hashlib.md5(s['url'].encode()).hexdigest()[:8]
        temp_data[sid] = SongData(id=sid, url=s['url'], title=s['title'], duration=s['duration'], artist=s['artist'])
        builder.button(text=f"{s['num']}", callback_data=f"dl_{sid}")
    builder.adjust(5)
    
    await message.answer(f"{result}👇 <b>Raqamni bosing</b>\n\n❤️ @zurnavolarbot", reply_markup=builder.as_markup())

# =================== CALLBACKS ===================
@dp.callback_query(F.data.startswith("mp3_"))
async def get_mp3(call: CallbackQuery):
    hid = call.data.replace("mp3_", "")
    info = video_cache.get(hid)
    if not info:
        await call.answer("❌", show_alert=True)
        return
    
    await call.answer("⏳")
    msg = await call.message.answer(f"⏳ MP3 tayyor...")
    
    try:
        filename, title = await asyncio.wait_for(download_mp3(info['url'], call.from_user.id), timeout=60)
    except:
        await msg.delete()
        await call.message.answer("❌ Vaqt tugadi")
        return
    
    await msg.delete()
    
    if filename and os.path.exists(filename):
        size = os.path.getsize(filename)
        artist, song_title = extract_artist_title(title)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{song_title[:45]}</b>\n📦 {format_size(size)}\n\n❤️ @zurnavolarbot",
            title=song_title[:60],
            performer=artist[:30] or "Zurnavolar"
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ {title[:100]}")

@dp.callback_query(F.data.startswith("sim_"))
async def similar_songs(call: CallbackQuery):
    hid = call.data.replace("sim_", "")
    info = video_cache.get(hid)
    if not info:
        await call.answer("❌", show_alert=True)
        return
    
    await call.answer("🔍")
    msg = await call.message.answer(f"🔍 {info['search'][:40]}...")
    
    songs = await search_songs(info['search'], limit=10)
    await msg.delete()
    
    if not songs:
        await call.message.answer("❌ Oxshash topilmadi")
        return
    
    result = f"🎵 <b>{info['search'][:40]}</b>\n\n"
    for i, s in enumerate(songs[:10], 1):
        if s['artist']:
            result += f"{i}. {s['artist']} — {s['title'][:35]}\n   ⏱ {s['duration']}\n\n"
        else:
            result += f"{i}. {s['title'][:40]}\n   ⏱ {s['duration']}\n\n"
    
    # TUGMALAR FAQAT RAQAM
    builder = InlineKeyboardBuilder()
    for i, s in enumerate(songs[:10], 1):
        sid = hashlib.md5(s['url'].encode()).hexdigest()[:8]
        temp_data[sid] = SongData(id=sid, url=s['url'], title=s['title'], duration=s['duration'], artist=s['artist'])
        builder.button(text=f"{i}", callback_data=f"dl_{sid}")
    builder.adjust(5)
    
    await call.message.answer(
        f"{result}\n━━━━━━━━━━━━━━━━\n🔍 {len(songs)} ta versiya\n━━━━━━━━━━━━━━━━\n👇 <b>Raqamni bosing</b>\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("dl_"))
async def download_song(call: CallbackQuery):
    sid = call.data.replace("dl_", "")
    song = temp_data.get(sid)
    if not song:
        await call.answer("❌", show_alert=True)
        return
    
    await call.answer("⏳")
    msg = await call.message.answer(f"⏳ {song.title[:35]}...")
    
    try:
        filename, title = await asyncio.wait_for(download_mp3(song.url, call.from_user.id), timeout=60)
    except:
        await msg.delete()
        await call.message.answer("❌ Vaqt tugadi")
        return
    
    await msg.delete()
    
    if filename and os.path.exists(filename):
        size = os.path.getsize(filename)
        artist, song_title = extract_artist_title(title)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{song_title[:45]}</b>\n📦 {format_size(size)}\n\n❤️ @zurnavolarbot",
            title=song_title[:60],
            performer=artist[:30] or "Zurnavolar"
        )
        os.remove(filename)
        temp_data.pop(sid, None)
    else:
        await call.message.answer(f"❌ {title[:100]}")

@dp.errors()
async def errors_handler(event, exception):
    logging.error(f"Xatolik: {exception}")
    return True

# =================== KEEP-ALIVE ===================
async def keep_alive():
    async def handler(r, w):
        try:
            await r.read(1024)
            body = json.dumps({"status": "alive", "time": int(time.time())})
            w.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{body}".encode())
            await w.drain()
        except:
            pass
        finally:
            w.close()
    server = await asyncio.start_server(handler, '0.0.0.0', Config.KEEP_ALIVE_PORT)
    print(f"🟢 Keep-Alive: {Config.KEEP_ALIVE_PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    async with aiohttp.ClientSession() as s:
        while bot_running:
            try:
                await s.get(url, timeout=5)
                print(f"✅ Ping: {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(300)

# =================== MAIN ===================
async def main():
    global bot_running
    logging.basicConfig(level=logging.INFO)
    
    try:
        me = await bot.get_me()
        print("=" * 40)
        print(f"🎵 Zurnavolar: @{me.username}")
        print(f"🎤 Shazam: {'✅' if SHAZAM_AVAILABLE else '❌'}")
        print(f"🎬 FFmpeg: {'✅' if shutil.which('ffmpeg') else '❌'}")
        print("=" * 40)
    except:
        pass
    
    asyncio.create_task(keep_alive())
    asyncio.create_task(self_ping())
    
    while bot_running:
        try:
            print("🚀 Bot ishga tushdi")
            await dp.start_polling(bot, allowed_updates=['message', 'callback_query'])
        except Exception as e:
            print(f"❌ {e} - 5 soniya keyin")
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
