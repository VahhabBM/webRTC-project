FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

# The primary index is unchanged. The fallbacks are only used when the one
# before them fails, so an unreachable mirror no longer breaks the build.
ARG PIP_INDEX_PRIMARY=https://mirror-pypi.runflare.com/simple
ARG PIP_INDEX_FALLBACKS="https://pypi.tuna.tsinghua.edu.cn/simple https://pypi.org/simple"

RUN set -eu; \
    for index in $PIP_INDEX_PRIMARY $PIP_INDEX_FALLBACKS; do \
        echo "==> installing requirements from $index"; \
        if pip install --no-cache-dir --default-timeout=100 --retries 2 \
            -i "$index" -r /app/requirements.txt; then \
            echo "==> requirements installed from $index"; \
            exit 0; \
        fi; \
        echo "==> index $index failed, trying the next one"; \
    done; \
    echo "==> every pip index failed" >&2; \
    exit 1
COPY . /app

RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]



