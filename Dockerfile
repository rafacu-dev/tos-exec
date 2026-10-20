# tos-exec en un servidor: login headless (Chromium) + API HTTP de ordenes.
#
#   docker build -t tos-exec .
#   docker run -p 5060:5060 -v tos-data:/data \
#     -e TOS_USERNAME=... -e TOS_PASSWORD=... -e TOS_ORDER_TOKEN=... tos-exec
#
# Los secretos llegan como variables de entorno del gestor de secretos del
# servidor; nunca se copian a la imagen. /data debe ser un disco persistente:
# guarda el token y el perfil del navegador (el "recordar este dispositivo").
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tos ./tos
RUN pip install --no-cache-dir ".[server]" \
    && python -m playwright install --with-deps chromium

ENV TOS_DATA_DIR=/data/tos \
    TOS_HTTP_HOST=0.0.0.0 \
    TOS_HTTP_PORT=5060 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 5060

CMD ["python", "-m", "tos.servicio"]
