FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 1001 sketch \
 && useradd --system --uid 1001 --gid sketch --home-dir /app --shell /usr/sbin/nologin sketch

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY --chown=sketch:sketch . .

USER sketch

ENTRYPOINT ["python", "entrypoint.py"]
