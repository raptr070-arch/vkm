import asyncio
import os
import re
import hashlib
import json
import time
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
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
from aiogram.enums import ParseMode, ChatAction
from aiogram.utils.keyboard import InlineKeyboardBuilder
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
import logging
import aiohttp

# ShazamIO (audio aniqlash uchun)
try:
    from shazamio import Shazam
    SHAZAM_AVAILABLE = True
except ImportError:
    SHAZAM_AVAILABLE = False
    print("⚠️ ShazamIO o'rnatilmagan. Audio aniqlash ishlamaydi!")

# =================== KONFIGURATSIYA ===================
load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DOWNLOADS_PATH = Path("downloads")
    TEMP_PATH = Path("temp_audio")
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
    AUDIO_SAMPLE_DURATION = 15  # Audio aniqlash uchun soniya

if not Config.BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! .env faylini tekshiring")

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

session = AiohttpSession(timeout=60)
bot = Bot(
    token=Config.BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
pool = ThreadPoolExecutor(max_workers=3)

# Vaqtinchalik ma'lumotlar
temp_data: Dict[str, SongData] = {}
video_cache: Dict[str, dict] = {}

# Shazam client
shazam = Shazam() if SHAZAM_AVAILABLE else None

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

def clean_filename(filename: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', filename)[:80]

def extract_artist_title(full_title: str):
    """Qo'shiq nomidan artist va titleni ajratish"""
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

# =================== AUDIO ANIQLASH (SHAZAM) ===================
async def identify_audio_from_video(video_path: str) -> Optional[dict]:
    """Video ichidagi audioni aniqlash"""
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
        try:
            opts = {
                'outtmpl': str(Config.DOWNLOADS_PATH / f"video_{user_id}_%(title)s.%(ext)s"),
                'format': 'best[height<=720][ext=mp4]/best[ext=mp4]',
                'quiet': True,
                'no_warnings': True,
                'retries': 3,
                'socket_timeout': 30,
                'merge_output_format': 'mp4',
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
            return None, str(e), 0

    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== MP3 YUKLASH ===================
async def download_mp3(url: str, user_id: int):
    def run():
        try:
            opts = {
                'outtmpl': str(Config.DOWNLOADS_PATH / f"audio_{user_id}_%(title)s.%(ext)s"),
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'no_warnings': True,
                'retries': 3,
                'socket_timeout': 30,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + ".mp3"
                title = info.get('title', 'Audio')
                return filename, title
        except Exception as e:
            return None, str(e)

    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== QO'SHIQ QIDIRISH ===================
async def search_songs(query: str, limit: int = 10) -> List[dict]:
    def run():
        try:
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
            }
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
        except:
            return []

    return await asyncio.get_event_loop().run_in_executor(pool, run)

# =================== START ===================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎵 <b>MP3kuylabot</b> 🎵\n\n"
        "📥 <b>Link yuboring:</b>\n"
        "YouTube | Instagram | TikTok | Facebook\n\n"
        "🔍 <b>Qo'shiq qidirish:</b>\n"
        "Masalan: yalla, shoxruxon\n\n"
        "🎯 <b>Instagram video</b> yuborsangiz,\n"
        "video ichidagi qo'shiqni Shazam orqali aniqlaydi!\n\n"
        "/help - Yordam",
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📚 <b>Yordam</b>\n\n"
        "1️⃣ Link yuboring yoki qo'shiq nomini yozing\n"
        "2️⃣ Video chiqsa, pastdagi tugmalarni bosing:\n"
        "   🎵 <b>MP3 yuklash</b> - videoni MP3 qiladi\n"
        "   🔍 <b>Oxshashlar</b> - cover/remix versiyalarni topadi\n"
        "3️⃣ Instagram/TikTok videolarida qo'shiqni Shazam aniqlaydi\n\n"
        "📞 @MP3kuylabot"
    )

# =================== XABARLAR ===================
@dp.message(F.text)
async def handle_message(message: Message):
    text = message.text.strip()
    user_id = message.from_user.id

    if re.match(r'^https?://', text):
        await process_url(message, text, user_id)
    else:
        await process_search(message, text, user_id)

async def process_url(message: Message, url: str, user_id: int):
    """Linkdan video yuklash"""
    platform = get_platform(url)

    if platform == 'other':
        await message.answer("❌ Faqat YouTube, Instagram, TikTok, Facebook linklari!")
        return

    status = await message.answer("⏳ <b>Video yuklanmoqda...</b>")

    filename, full_title, duration = await download_video(url, user_id)

    await status.delete()

    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)

        if file_size > Config.MAX_FILE_SIZE:
            await message.answer(f"❌ Video juda katta! Hajmi: {format_size(file_size)}")
            os.remove(filename)
            return

        url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
        artist, title = extract_artist_title(full_title)

        # Audio aniqlash (Instagram, TikTok, Facebook uchun)
        identified_song = None
        if platform in ['instagram', 'tiktok', 'facebook']:
            detect_msg = await message.answer("🎵 <b>Shazam orqali qo'shiq aniqlanmoqda...</b>")
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

        platform_emoji = {
            'youtube': '🎬', 'instagram': '📸',
            'tiktok': '🎵', 'facebook': '📘'
        }

        video_file = FSInputFile(filename)

        caption = f"{platform_emoji.get(platform, '📹')} <b>{full_title[:50]}</b>\n⏱️ {format_duration(duration)}"
        if identified_song:
            caption += f"\n\n🎯 <b>Shazam aniqladi:</b>\n{identified_song['full_title'][:80]}"

        await message.answer_video(video_file, caption=caption, reply_markup=keyboard)
        os.remove(filename)
    else:
        await message.answer(f"❌ Yuklab bo'lmadi!\nSabab: {full_title[:100]}")

async def process_search(message: Message, query: str, user_id: int):
    """Qo'shiq qidirish"""
    status = await message.answer(f"🔍 <b>Qidirilmoqda:</b> {query}...")

    songs = await search_songs(query, limit=10)

    await status.delete()

    if not songs:
        await message.answer("❌ Hech narsa topilmadi! Boshqacha qidirib ko'ring.")
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
            btn_text = f"{song['number']}. {song['artist'][:25]} — {song['title'][:25]}"
        else:
            btn_text = f"{song['number']}. {song['title'][:40]}"
        builder.button(text=btn_text, callback_data=f"dl_{song_id}")

    builder.adjust(1)

    await message.answer(
        f"🎵 <b>Qidiruv natijasi: <code>{query}</code></b>\n\n"
        f"{songs_text}"
        f"👇 <b>Yuklab olish uchun tanlang:</b>",
        reply_markup=builder.as_markup()
    )

# =================== VIDEO OSTIDAGI MP3 TUGMASI ===================
@dp.callback_query(F.data.startswith("mp3_"))
async def mp3_from_video(call: CallbackQuery):
    """Videoni MP3 qilib yuklash"""
    url_hash = call.data.replace("mp3_", "")

    video_info = video_cache.get(url_hash)
    if not video_info:
        try:
            await call.answer("❌ Video ma'lumoti topilmadi!", show_alert=True)
        except:
            pass
        return

    try:
        await call.answer("⏳ MP3 yuklanmoqda...")
    except:
        pass

    if video_info.get('identified_song'):
        display_title = video_info['identified_song']['full_title'][:50]
    else:
        display_title = video_info['title'][:50]

    status = await call.message.answer(f"⏳ <b>MP3 tayyorlanmoqda:</b> {display_title}...")

    filename, title = await download_mp3(video_info['url'], call.from_user.id)

    await status.delete()

    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        artist, song_title = extract_artist_title(title)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @MP3kuylabot",
            title=song_title[:64],
            performer=artist[:64] if artist else "MP3kuylabot"
        )
        os.remove(filename)
    else:
        await call.message.answer(f"❌ MP3 yuklab bo'lmadi!\nSabab: {title[:100]}")

# =================== OXSHASH QO'SHIQLAR ===================
@dp.callback_query(F.data.startswith("similar_"))
async def similar_songs(call: CallbackQuery):
    """Oxshash qo'shiqlar ro'yxati"""
    url_hash = call.data.replace("similar_", "")

    video_info = video_cache.get(url_hash)
    if not video_info:
        try:
            await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        except:
            pass
        return

    try:
        await call.answer("🔍 Qidirilmoqda...")
    except:
        pass

    if video_info.get('identified_song'):
        search_query = video_info['identified_song']['full_title']
        artist = video_info['identified_song'].get('artist', '')
        song_title = video_info['identified_song'].get('title', '')
        detection_method = "🎯 Shazam orqali"
    else:
        artist = video_info.get('artist', '')
        song_title = video_info.get('clean_title', '')
        search_query = f"{artist} {song_title}".strip()
        detection_method = "📋 Video sarlavhasi orqali"

    status = await call.message.answer(
        f"🔍 <b>Oxshash qo'shiqlar qidirilmoqda:</b>\n"
        f"{detection_method}: {search_query[:60]}..."
    )

    all_songs = []
    seen_urls = set()

    # 1. Aniq qo'shiq nomi bilan qidirish
    if search_query:
        songs1 = await search_songs(search_query, limit=10)
        for s in songs1:
            if s['url'] not in seen_urls:
                all_songs.append(s)
                seen_urls.add(s['url'])

    # 2. Cover versiyalari
    if song_title:
        cover_query = f"{song_title} cover version"
        songs2 = await search_songs(cover_query, limit=5)
        for s in songs2:
            if s['url'] not in seen_urls:
                all_songs.append(s)
                seen_urls.add(s['url'])

    # 3. Remix versiyalari
    if song_title:
        remix_query = f"{song_title} remix"
        songs3 = await search_songs(remix_query, limit=5)
        for s in songs3:
            if s['url'] not in seen_urls:
                all_songs.append(s)
                seen_urls.add(s['url'])

    # 4. Faqat artist bo'yicha
    if len(all_songs) < 5 and artist:
        songs4 = await search_songs(artist, limit=5)
        for s in songs4:
            if s['url'] not in seen_urls:
                all_songs.append(s)
                seen_urls.add(s['url'])

    await status.delete()

    if not all_songs:
        await call.message.answer("❌ Oxshash qo'shiqlar topilmadi!")
        return

    display_songs = all_songs[:10]

    header = f"🎵 <b>{search_query[:50]}</b>"

    songs_text = ""
    for s in display_songs:
        if s['artist']:
            songs_text += f"\n{s['number']}. {s['artist']} — {s['title'][:50]}  <code>{s['duration']}</code>"
        else:
            songs_text += f"\n{s['number']}. {s['title'][:50]}  <code>{s['duration']}</code>"

    builder = InlineKeyboardBuilder()
    for song in display_songs:
        song_id = hashlib.md5(song['url'].encode()).hexdigest()[:10]
        temp_data[song_id] = SongData(
            id=song_id, url=song['url'], title=song['full_title'],
            duration=song['duration'], artist=song['artist'], platform='youtube'
        )
        _, btn_title = extract_artist_title(song['full_title'])
        if song['artist']:
            btn_text = f"{song['number']}. {song['artist'][:20]} — {btn_title[:25]}"
        else:
            btn_text = f"{song['number']}. {btn_title[:45]}"
        builder.button(text=btn_text, callback_data=f"dl_{song_id}")

    builder.adjust(1)

    await call.message.answer(
        f"{header}\n\n"
        f"{songs_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 <b>Topildi:</b> {len(all_songs)} ta versiya\n"
        f"📌 Cover | Remix | Boshqa artistlar\n"
        f"{detection_method} aniqlandi\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"❤️ @MP3kuylabot\n\n"
        f"👇 <b>Yuklab olish uchun tanlang:</b>",
        reply_markup=builder.as_markup()
    )

# =================== TANLANGAN QO'SHIQNI YUKLASH ===================
@dp.callback_query(F.data.startswith("dl_"))
async def download_selected(call: CallbackQuery):
    """Ro'yxatdan tanlangan qo'shiqni MP3 yuklash"""
    song_id = call.data.replace("dl_", "")

    song_data = temp_data.get(song_id)
    if not song_data:
        try:
            await call.answer("❌ Ma'lumot topilmadi!", show_alert=True)
        except:
            pass
        return

    try:
        await call.answer("⏳ MP3 yuklanmoqda...")
    except:
        pass

    status = await call.message.answer(f"⏳ <b>MP3 tayyorlanmoqda:</b> {song_data.title[:40]}...")

    filename, title = await download_mp3(song_data.url, call.from_user.id)

    await status.delete()

    if filename and os.path.exists(filename):
        file_size = os.path.getsize(filename)
        artist, song_title = extract_artist_title(title)
        await call.message.answer_audio(
            FSInputFile(filename),
            caption=f"🎵 <b>{title[:50]}</b>\n📦 {format_size(file_size)}\n\n❤️ @MP3kuylabot",
            title=song_title[:64],
            performer=artist[:64] if artist else "MP3kuylabot"
        )
        os.remove(filename)
        temp_data.pop(song_id, None)
    else:
        await call.message.answer(f"❌ MP3 yuklab bo'lmadi!\nSabab: {title[:100]}")

# =================== XATOLIKLAR ===================
@dp.errors()
async def errors_handler(event, exception):
    error_msg = str(exception)
    if "message is not modified" not in error_msg.lower():
        logging.error(f"Xatolik: {exception}")
    return True

# =================== MAIN ===================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    bot_info = await bot.get_me()

    print("=" * 50)
    print(f"🎵 MP3kuylabot ishga tushdi!")
    print(f"🤖 Bot: @{bot_info.username}")
    print(f"🎯 Audio aniqlash: {'✅ Shazam mavjud' if SHAZAM_AVAILABLE else '❌ Shazam yoq'}")
    print("=" * 50)

    # Render.com uchun oddiy polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
