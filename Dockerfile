FROM python:3.12-slim

# FFmpeg va Rust o'rnatish
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    build-essential \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && apt-get clean

ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "boot.py"]
# Dockerfile ga qo'shing
RUN pip install --upgrade yt-dlp
