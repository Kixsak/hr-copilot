FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=CST-8 \
    HRC_DATA_DIR=/data \
    HRC_CLEAN_DIR=/state/work \
    HRC_LOG_DIR=/state/logs \
    HRC_USERS_FILE=/state/users.json

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore 排除 work/、logs/、users.json、.env、評測輸出等：映像不含人事資料、帳號或金鑰
COPY . .
RUN useradd --create-home app && mkdir -p /state && chown app:app /state
USER app

EXPOSE 8501
CMD ["python", "docker_entry.py"]
