FROM python:3.11-slim

WORKDIR /app

# Системные зависимости (Tesseract, fonts)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

# Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY spectrum/ spectrum/
COPY README.md ./

# Директория для данных
RUN mkdir -p /app/knowledge_base /app/data

EXPOSE 8118

CMD ["python3", "-m", "uvicorn", "spectrum.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8118"]
