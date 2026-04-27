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
    DOWNLOAD_TIMEOUT = 60  # 1 daqiqa
    MAX_RETRIES = 3

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

# =================== INITIALIZATION ===================
Config.DOWNLOADS_PATH.mkdir(exist_ok=True)
Config.TEMP_PATH.mkdir(exist_ok=True)

# Bot session with longer timeout
session = AiohttpSession(timeout=120)
bot = Bot(
    token=Config.BOT_TOKEN, 
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=2)  # Kamaytirildi

temp_data: Dict[str, SongData] = {}
video_cache: Dict[str, dict] = {}
shazam = Shazam() if SHAZAM_AVAILABLE else None
bot_running = True

# Cookie fayl yo'q - oddiy sozlamalar
YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'ignoreerrors': True,
    'extract_flat': False,
    'retries': 3,
    'fragment_retries': 3,
    'socket_timeout': 30,
}

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
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

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
    clean_title = re.sub(r'\(.*?\)', '', clean_title)
    clean_title = re.sub(r'\[.*?\]', '', clean_title)
    
    remove_words = [
        'Official Video', 'Official Music Video', 'Official Audio',
        'MV', 'M/V', 'Music Video', 'Lyrics',
        'HD', '4K', '1080p', '720p',
        'TikTok', 'Trend', 'Viral', '2024', '2025', '2026',
        'Cover', 'AI Cover', 'Remix',
    ]
    
    for word in remove_words:
        clean_title = re.sub(re.escape(word), '', clean_title, flags=re.IGNORECASE)
    
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
    clean_artist = re.sub(r'\s+', ' ', clean_artist).strip()
    
    if not clean_title or len(clean_title) < 3:
        clean_title = title.strip()
    
    return clean_artist, clean_title

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

# =================== VIDEO YUKLASH (COOKIESIZ) ===================
async def download_video(url: str, user_id: int):
    def run():
        for attempt in range(Config.MAX_RETRIES):
            try:
                opts = {
                    **YDL_OPTS,
                    'outtmpl': str(Config.DOWNLOADS_PATH / f"video_{user_id}_{int(time.time())}_%(title)s.%(ext)s"),
                    'format': 'best[height<=480][ext=mp4]/best[height<=720][ext=mp4]/best[ext=mp4]',
                    'merge_output_format': 'mp4',
                    'geo_bypass': True,
                    'extractor_retries': 3,
                }
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
                if attempt == Config.MAX_RETRIES - 1:
                    return None, str(e), 0
                time.sleep(2)
        return None, "Unknown error", 0
    
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== MP3 YUKLASH (COOKIESIZ) ===================
async def download_mp3(url: str, user_id: int):
    def run():
        for attempt in range(Config.MAX_RETRIES):
            try:
                opts = {
                    **YDL_OPTS,
                    'outtmpl': str(Config.DOWNLOADS_PATH / f"audio_{user_id}_{int(time.time())}_%(title)s.%(ext)s"),
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                    'geo_bypass': True,
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                    title = info.get('title', 'Audio')
                    return filename, title
            except Exception as e:
                if attempt == Config.MAX_RETRIES - 1:
                    return None, str(e)
                time.sleep(2)
        return None, "Unknown error"
    
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== QO'SHIQ QIDIRISH ===================
async def search_songs(query: str, limit: int = 8) -> List[dict]:
    def run():
        try:
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'ignoreerrors': True,
                'geo_bypass': True,
            }
            search_query = f"ytsearch{limit}:{query}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                songs = []
                if 'entries' in info:
                    for i, item in enumerate(info['entries'], 1):
                        if item and item.get('id'):
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
            logging.error(f"Qidiruv xatoligi: {e}")
            return []
    
    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== HANDLERS ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>Zurnavolar Bot</b> 🎵\n\n"
        "📥 <b>Link yuboring:</b>\n"
        "YouTube | Instagram | TikTok | Facebook\n\n"
        "🔍 <b>Qo'shiq qidirish:</b>\n"
        "Masalan: yalla, shoxruxon\n\n"
        "/help - Yordam\n\n"
        "@zurnavolarbot"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Yordam</b>\n\n"
        "🎵 <b>Qanday ishlatiladi?</b>\n"
        "1. YouTube/Instagram/TikTok/Facebook linkini yuboring\n"
        "2. Qo'shiq nomini yozib qidiring\n"
        "3. Instagram video yuborsangiz, avtomatik qo'shiq aniqlanadi\n\n"
        "⚙️ <b>Buyruqlar:</b>\n"
        "/start - Botni qayta ishga tushirish\n"
        "/help - Yordam\n\n"
        "@zurnavolarbot"
    )

@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    user_id = message.from_user.id
    
    if re.match(r'^https?://', text):
        await process_url(message, text, user_id)
    else:
        if len(text) < 3:
            await message.answer("❌ Qidiruv uchun kamida 3 harf yozing!")
            return
        await process_search(message, text, user_id)

async def process_url(message: Message, url: str, user_id: int):
    platform = get_platform(url)
    
    if platform == 'other':
        await message.answer("❌ Faqat YouTube, Instagram, TikTok, Facebook linklari!")
        return
    
    status = await message.answer("⏳ <b>Video yuklanmoqda (1 daqiqagacha vaqt ketishi mumkin)...</b>")
    
    try:
        filename, full_title, duration = await asyncio.wait_for(
            download_video(url, user_id), 
            timeout=Config.DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        await status.delete()
        await message.answer("❌ Yuklash vaqti tugadi! Keyinroq urunib ko'ring.")
        return
    
    await status.delete()
    
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        
        if file_size > Config.MAX_FILE_SIZE:
            await message.answer(f"❌ Video juda katta! Hajmi: {format_size(file_size)}")
            os.remove(filename)
            return
        
        url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
        artist, title = extract_artist_title(full_title)
        
        identified_song = None
        if platform in ['instagram', 'tiktok', 'facebook']:
            detect_msg = await message.answer("🎵 <b>Video ichidagi qo'shiq aniqlanmoqda...</b>")
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
            [InlineKeyboardButton(text="🎵 MP3 yuklash", callback_data=f"mp3_{url_hash}")],
            [InlineKeyboardButton(text="🔍 Oxshashlar", callback_data=f"similar_{url_hash}")]
        ])
        
        platform_emoji = {'youtube': '🎬', 'instagram': '📸', 'tiktok': '🎵', 'facebook': '📘'}
        
        try:
            video_file = FSInputFile(filename)
            caption = f"{platform_emoji.get(platform, '📹')} <b>{full_title[:50]}</b>\n⏱️ {format_duration(duration)}"
            if identified_song:
                caption += f"\n\n🎯 <b>Aniqlangan qo'shiq:</b>\n{identified_song['full_title'][:80]}"
            caption += f"\n\n❤️ @zurnavolarbot"
            
            await message.answer_video(video_file, caption=caption, reply_markup=keyboard)
        except Exception as e:
            await message.answer(f"❌ Video jo'natishda xatolik: {str(e)[:100]}")
        
        try:
            os.remove(filename)
        except:
            pass
    else:
        await message.answer(f"❌ Yuklab bo'lmadi!\nSabab: {full_title[:100]}")

async def process_search(message: Message, query: str, user_id: int):
    status = await message.answer(f"🔍 <b>Qidirilmoqda:</b> {query}...")
    songs = await search_songs(query, limit=8)
    await status.delete()
    
    if not songs:
        await message.answer("❌ Hech narsa topilmadi!")
        return
    
    songs_text = ""
    for s in songs:
        if s['artist']:
            songs_text += f"{s['number']}. {s['artist']} — {s['title']}\n   ⏱ {s['duration']}\n\n"
        else:
            songs_text += f"{s['number']}. {s['title']}\n   ⏱ {s['duration']}\n\n"
    
    builder = InlineKeyboardBuilder()
    for song in songs:
        song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
        temp_data[song_id] = SongData(
            id=song_id, url=song['url'], title=song['full_title'],
            duration=song['duration'], artist=song['artist'], platform='youtube'
        )
        if song['artist']:
            btn_text = f"{song['number']}. {song['artist'][:20]}"
        else:
            btn_text = f"{song['number']}. {song['title'][:30]}"
        builder.button(text=btn_text, callback_data=f"dl_{song_id}")
    
    builder.adjust(1)
    await message.answer(
        f"🎵 <b>Qidiruv natijasi: <code>{query}</code></b>\n\n{songs_text}👇 <b>Yuklab olish uchun tanlang:</b>\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    url_hash = call.data.replace("mp3_", "")
    video_info = video_cache.get(url_hash)
    
    if not video_info:
        await call.answer("❌ Video ma'lumoti topilmadi!", show_alert=True)
        return
    
    await call.answer("⏳ MP3 yuklanmoqda...")
    
    display_title = video_info.get('identified_song', {}).get('full_title', video_info['title'])[:50]
    status = await call.message.answer(f"⏳ <b>MP3 tayyorlanmoqda:</b> {display_title}...")
    
    try:
        filename, title = await asyncio.wait_for(
            download_mp3(video_info['url'], call.from_user.id),
            timeout=Config.DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        await status.delete()
        await call.message.answer("❌ MP3 yuklash vaqti tugadi!")
        return
    
    await status.delete()
    
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        try:
            await call.message.answer_audio(
                FSInputFile(filename),
                caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @zurnavolarbot",
                title=title[:64],
                performer="Zurnavolar"
            )
        except Exception as e:
            await call.message.answer(f"❌ MP3 jo'natishda xatolik: {str(e)[:100]}")
        
        try:
            os.remove(filename)
        except:
            pass
    else:
        await call.message.answer(f"❌ MP3 yuklab bo'lmadi!\nSabab: {title[:100]}")

@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    url_hash = call.data.replace("similar_", "")
    video_info = video_cache.get(url_hash)
    
    if not video_info:
        await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        return
    
    await call.answer("🔍 Qidirilmoqda...")
    
    if video_info.get('identified_song'):
        search_query = video_info['identified_song']['full_title']
        song_title = video_info['identified_song'].get('title', '')
    else:
        song_title = video_info.get('clean_title', '')
        search_query = f"{video_info.get('artist', '')} {song_title}".strip()
    
    status = await call.message.answer(f"🔍 <b>Oxshash qo'shiqlar qidirilmoqda:</b> {search_query[:50]}...")
    
    all_songs = []
    seen_urls = set()
    
    if search_query and search_query.strip():
        songs1 = await search_songs(search_query, limit=8)
        for s in songs1:
            if s['url'] not in seen_urls:
                all_songs.append(s)
                seen_urls.add(s['url'])
    
    if song_title and song_title.strip() and len(all_songs) < 5:
        for q in [f"{song_title} cover", f"{song_title} remix"]:
            songs = await search_songs(q, limit=3)
            for s in songs:
                if s['url'] not in seen_urls:
                    all_songs.append(s)
                    seen_urls.add(s['url'])
    
    await status.delete()
    
    if not all_songs:
        await call.message.answer("❌ Oxshash qo'shiqlar topilmadi!")
        return
    
    display_songs = all_songs[:10]
    songs_text = ""
    for idx, s in enumerate(display_songs, 1):
        if s['artist']:
            songs_text += f"\n{idx}. {s['artist']} — {s['title'][:50]}  <code>{s['duration']}</code>"
        else:
            songs_text += f"\n{idx}. {s['title'][:50]}  <code>{s['duration']}</code>"
    
    builder = InlineKeyboardBuilder()
    for idx, song in enumerate(display_songs, 1):
        song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
        temp_data[song_id] = SongData(
            id=song_id, url=song['url'], title=song['full_title'],
            duration=song['duration'], artist=song['artist'], platform='youtube'
        )
        # FAQAT RAQAM
        btn_text = f"{idx}"
        builder.button(text=btn_text, callback_data=f"dl_{song_id}")
    
    builder.adjust(5)
    await call.message.answer(
        f"🎵 <b>{search_query[:50]}</b>\n\n{songs_text}\n\n━━━━━━━━━━━━━━━━━━━━━\n🔍 <b>Topildi:</b> {len(all_songs)} ta versiya\n━━━━━━━━━━━━━━━━━━━━━\n\n👇 <b>Raqamni bosing:</b>\n\n❤️ @zurnavolarbot",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    song_id = call.data.replace("dl_", "")
    song_data = temp_data.get(song_id)
    
    if not song_data:
        await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        return
    
    await call.answer("⏳ MP3 yuklanmoqda...")
    status = await call.message.answer(f"⏳ <b>MP3 tayyorlanmoqda:</b> {song_data.title[:40]}...")
    
    try:
        filename, title = await asyncio.wait_for(
            download_mp3(song_data.url, call.from_user.id),
            timeout=Config.DOWNLOAD_TIMEOUT
        )
    except asyncio.TimeoutError:
        await status.delete()
        await call.message.answer("❌ MP3 yuklash vaqti tugadi!")
        return
    
    await status.delete()
    
    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        artist, song_title = extract_artist_title(title)
        try:
            await call.message.answer_audio(
                FSInputFile(filename),
                caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @zurnavolarbot",
                title=song_title[:64],
                performer=artist[:64] if artist else "Zurnavolar"
            )
        except Exception as e:
            await call.message.answer(f"❌ MP3 jo'natishda xatolik: {str(e)[:100]}")
        
        try:
            os.remove(filename)
            temp_data.pop(song_id, None)
        except:
            pass
    else:
        await call.message.answer(f"❌ MP3 yuklab bo'lmadi!\nSabab: {title[:100]}")

@dp.errors()
async def errors_handler(event, exception):
    error_msg = str(exception)
    if "message is not modified" not in error_msg.lower():
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
    print(f"🟢 Keep-Alive server: 0.0.0.0:{Config.KEEP_ALIVE_PORT}")
    async with server:
        await server.serve_forever()

async def self_ping():
    await asyncio.sleep(30)
    ping_url = f"http://127.0.0.1:{Config.KEEP_ALIVE_PORT}"
    async with aiohttp.ClientSession() as session:
        while bot_running:
            try:
                async with session.get(ping_url, timeout=10) as resp:
                    if resp.status == 200:
                        print(f"✅ Self-Ping OK: {datetime.now().strftime('%H:%M:%S')}")
            except:
                pass
            await asyncio.sleep(Config.PING_INTERVAL)

# =================== MAIN ===================
async def main():
    global bot_running
    logging.basicConfig(level=logging.INFO)
    
    if os.getenv("RAILWAY_ENVIRONMENT"):
        print("🚂 Railway muhiti")
    
    try:
        bot_info = await bot.get_me()
        print("=" * 50)
        print(f"🎵 Zurnavolar Bot: @{bot_info.username}")
        print(f"🎵 Audio aniqlash: {'✅' if SHAZAM_AVAILABLE else '❌'}")
        print(f"🎬 FFmpeg: {'✅' if shutil.which('ffmpeg') else '❌'}")
        print("=" * 50)
    except:
        pass
    
    asyncio.create_task(keep_alive_server())
    asyncio.create_task(self_ping())
    
    while bot_running:
        try:
            print("🚀 Bot ishga tushdi...")
            await dp.start_polling(bot, allowed_updates=['message', 'callback_query'])
        except Exception as e:
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
