# Playwright official image — đã có sẵn Chromium + tất cả dependencies
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Cài Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ source
COPY . .

CMD ["python", "main.py"]
