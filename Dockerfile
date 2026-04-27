FROM python:3.12-slim

# FFmpeg o'rnatish (Shazam uchun kerak)
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean

WORKDIR /app

# Cookie faylni nusxalash
COPY cookies.txt .

# Kerakli fayllar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot fayli
COPY bot.py .

# Botni ishga tushirish
CMD ["python", "bot.py"]
