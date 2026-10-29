# harag PDF RAG MVP — API/워커 이미지
FROM python:3.12-slim

# pdfplumber + LibreOffice(.doc→docx 변환, ENABLE_DOC_CONVERT)
WORKDIR /app

# Debian 미러 Hash Sum mismatch 대비: 파이프라인 비활성 + 재시도.
RUN set -eux; \
    printf 'Acquire::http::Pipeline-Depth "0";\nAcquire::Retries "5";\n' \
      > /etc/apt/apt.conf.d/80-retries; \
    for i in 1 2 3 4 5; do \
      apt-get clean; \
      rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/partial; \
      apt-get update && \
      apt-get install -y --no-install-recommends libreoffice-writer-nogui \
      && break; \
      echo "apt install attempt $i failed; retrying..."; \
      sleep $((i * 3)); \
    done; \
    dpkg -l libreoffice-writer-nogui >/dev/null; \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir . && pip install --no-cache-dir "uvicorn[standard]>=0.27"

COPY frontend ./frontend
# 공용 설명서 시드·PDF 서빙. 없으면 기동 시드가 스킵되어 UI「설명서 0건」.
COPY demo_pdfs ./demo_pdfs
COPY docker/entrypoint.sh /entrypoint.sh

# 비루트 실행(설계 원칙: uid 10001). entrypoint가 volume chown 후 drop.
RUN useradd -u 10001 -m appuser \
    && mkdir -p /app/data /app/spool \
    && chown -R appuser:appuser /app \
    && chmod +x /entrypoint.sh

ENV FRONTEND_DIR=/app/frontend \
    PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "harag.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
