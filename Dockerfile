FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && apt-get clean

WORKDIR /app

# ========== COOKIE FAYLINI NUSXALASH ==========
# cookies.txt faylini loyiha papkasidan nusxalash
COPY cookies.txt .

# Tekshirish
RUN ls -la cookies.txt && head -n 3 cookies.txt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "boot.py"]
