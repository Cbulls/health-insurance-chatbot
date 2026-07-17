# harag PDF RAG MVP — API/워커 이미지
FROM python:3.12-slim

# pdfplumber + LibreOffice(.doc→docx 변환, ENABLE_DOC_CONVERT)
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir . && pip install --no-cache-dir "uvicorn[standard]>=0.27"

COPY frontend ./frontend

# 비루트 실행(설계 원칙: uid 10001)
RUN useradd -u 10001 -m appuser && chown -R appuser /app
USER 10001

ENV FRONTEND_DIR=/app/frontend \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "harag.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
