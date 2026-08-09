# ---------- build ----------
FROM python:3.14-slim AS build
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# métadonnées + code (hatchling a besoin du package pour builder)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# ---------- runtime ----------
FROM python:3.14-slim AS runtime
# ffmpeg = ré-encodage audio ; tini = init PID 1 propre (signaux/zombies)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
# utilisateur non-root
RUN useradd -m -u 1000 toumai
USER toumai
WORKDIR /home/toumai/app
# volume pour le stockage local (si STORAGE_BACKEND=local)
ENV TOUMAI_DATA_DIR=/data
ENTRYPOINT ["tini", "--"]
# par défaut : l'API. Le worker surcharge la commande (voir compose).
CMD ["toumai-api"]
