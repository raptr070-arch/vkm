FROM python:3.12-slim

# FFmpeg o'rnatish
RUN apt-get update && apt-get install -y ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Ishchi papka
WORKDIR /app

# Kerakli papkalarni yaratish
RUN mkdir -p downloads temp_audio

# Python fayllarni nusxalash
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot fayllarni nusxalash
COPY bot.py .
COPY .env .

# Cookie fayllarni nusxalash
COPY cookies.txt .
COPY instagram_cookies.txt .

# Port ochish
EXPOSE 8080

# Botni ishga tushirish
CMD ["python", "bot.py"]
