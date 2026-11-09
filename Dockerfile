# grc-tracker on Cloud Run (DESIGN D1): one small container, non-root, one uvicorn worker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Hong_Kong \
    PORT=8080

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY prompts ./prompts

RUN useradd --system --uid 10001 --no-create-home grc && chown -R grc /srv
USER grc

EXPOSE 8080
# One worker: the in-process write lock is only a lock within one process (D1).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --no-access-log"]
