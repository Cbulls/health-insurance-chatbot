# harag PDF RAG MVP — API 이미지
FROM python:3.12-slim

# pdfplumber(pdfminer)용 최소 런타임. 빌드 캐시 최적화를 위해 의존성 먼저 설치.
WORKDIR /app

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
