# 1. Asosiy image: Python 3.14
FROM python:3.14-slim

# 2. Rust kompilyatorini o‘rnatish
RUN apt-get update && apt-get install -y curl build-essential \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    && apt-get clean

# 3. Rust ni PATH ga qo‘shish
ENV PATH="/root/.cargo/bin:${PATH}"

# 4. Ishchi papkani yaratish va o‘tish
WORKDIR /app

# 5. requirements.txt ni nusxalash va paketlarni o‘rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Butun loyiha kodlarini nusxalash
COPY . .

# 7. Botni ishga tushirish buyrug‘i
CMD ["python", "boot.py"]