FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && apt-get clean

WORKDIR /app

# Cookie faylni nusxalash
COPY cookies.txt .

# Tekshirish (ixtiyoriy)
RUN ls -la cookies.txt && head -n 3 cookies.txt

# Qolgan fayllar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
