# The Safe Route Planner: the server, which also serves the page.
# The maps, saved answers and keys stay outside the image: see docker-compose.yml.
FROM rust:1-slim AS kernel
WORKDIR /kernel
COPY backend/rust_kernel .
RUN cargo build --release

FROM python:3.14-slim

# School zones, the sun and departure times all go by the clock here, and they're Milwaukee's.
ENV TZ=America/Chicago
# The route search and geometry, built above: the image needs no toolchain.
ENV ROUTES_KERNEL_LIBRARY=/srv/routes/libroute_kernel.so

WORKDIR /srv/routes/backend

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app app
COPY frontend ../frontend
COPY --from=kernel /kernel/target/release/libroute_kernel.so ../libroute_kernel.so

RUN useradd --create-home --uid 1000 routes && mkdir data .cache && chown routes:routes data .cache
USER routes

EXPOSE 3143
# The maps load before the server answers: seconds, more on a busy computer.
HEALTHCHECK --interval=30s --timeout=20s --start-period=2m --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3143/health', timeout=15)"]

# One process by default. WEB_CONCURRENCY=2 adds a worker, if there's memory for it (EXPO.md).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3143"]
