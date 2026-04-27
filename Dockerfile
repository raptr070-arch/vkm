FROM python:3.12-slim

# FFmpeg o'rnatish
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean

# Ishchi papka
WORKDIR /app

# Cookie va talab fayllarini nusxalash
COPY cookies.txt .
COPY requirements.txt .

# Python paketlarni o'rnatish
RUN pip install --no-cache-dir -r requirements.txt

# Bot faylini nusxalash
COPY bot.py .

# Botni ishga tushirish
CMD ["python", "bot.py"]
