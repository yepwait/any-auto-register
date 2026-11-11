# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS frontend-builder

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

ARG CAMOUFOX_VERSION=152.0.4
ARG CAMOUFOX_RELEASE=beta.30

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_CONDA_ENV=docker \
    APP_RELOAD=0 \
    APP_RUNTIME_DIR=/runtime \
    CREDENTIAL_ENCRYPTION_KEY_FILE=/runtime/.secrets/credential_key \
    APP_ENABLE_SOLVER=0 \
    SOLVER_PORT=8889 \
    SOLVER_BIND_HOST=0.0.0.0 \
    LOCAL_SOLVER_URL=http://127.0.0.1:8889 \
    SOLVER_BROWSER_TYPE=camoufox \
    SOLVER_THREADS=1

WORKDIR /app

COPY requirements.txt ./
COPY scripts/install_camoufox.py /tmp/install_camoufox.py

# nodejs 不是构建期依赖：ChatGPT 的 Sentinel PoW 求解器要在运行时起 node 子进程
# 跑 OpenAI 的 sdk.js，缺它注册链路会静默收不到验证码。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates nodejs \
        libgtk-3-0 libx11-xcb1 libasound2 xvfb xauth \
    && curl -fsSL https://go.dev/dl/go1.24.2.linux-amd64.tar.gz | tar -C /usr/local -xz \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/go/bin:/root/.local/bin:${PATH}"

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && installed=0 \
    && for attempt in 1 2 3; do \
         if python -m playwright install --with-deps chromium firefox; then \
           installed=1; \
           break; \
         fi; \
         if [ "$attempt" -eq 3 ]; then break; fi; \
         echo "playwright browser install failed, retrying ($attempt/3)..." >&2; \
         sleep 5; \
       done \
    && [ "$installed" -eq 1 ] \
    && CAMOUFOX_VERSION="$CAMOUFOX_VERSION" CAMOUFOX_RELEASE="$CAMOUFOX_RELEASE" python /tmp/install_camoufox.py

COPY . .
COPY --from=frontend-builder /app/static /app/static

# 运行时不需要额外工具包；基础镜像已有 sed/chmod，避免在小磁盘服务器上重复安装 100MB 级依赖。
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh \
    && chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /runtime /runtime/logs /_ext_targets

EXPOSE 8000 8889

VOLUME ["/runtime", "/_ext_targets"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
