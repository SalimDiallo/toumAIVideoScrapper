# TOUMAI — raccourcis de dev. À lancer depuis Git Bash.
#   make            -> aide
#   make install    -> venv + dépendances
#   make up         -> infra (postgres + minio + kafka)
#   make api        -> API FastAPI    (terminal 1)
#   make worker     -> worker Kafka   (terminal 2)
#   make ingest URL="https://youtu.be/xxxx" LANGS=fr

SHELL := bash
VENV := .venv/Scripts
PY := $(VENV)/python.exe
COMPOSE := docker compose

LANGS ?= fr

.DEFAULT_GOAL := help
.PHONY: help install test lint fmt up down ps logs api worker ingest clean

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Environnement --------------------------------------------------------
install: ## Crée le venv et installe le projet (+ dev)
	python -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

test: ## Lance les tests
	$(PY) -m pytest -q

lint: ## Vérifie le style (ruff + black --check)
	$(PY) -m ruff check src tests
	$(PY) -m black --check src tests

fmt: ## Formate le code (black + ruff --fix)
	$(PY) -m black src tests
	$(PY) -m ruff check --fix src tests

# --- Infra Docker ---------------------------------------------------------
up: ## Démarre postgres + minio + kafka
	$(COMPOSE) up -d

down: ## Arrête l'infra
	$(COMPOSE) down

ps: ## État des conteneurs
	$(COMPOSE) ps

logs: ## Suit les logs de l'infra
	$(COMPOSE) logs -f

clean: ## Arrête tout et supprime les volumes (⚠ données)
	$(COMPOSE) down -v

# --- Application ----------------------------------------------------------
api: ## Lance l'API FastAPI (http://localhost:8000/docs)
	$(VENV)/toumai-api.exe

worker: ## Lance un worker Kafka
	$(VENV)/toumai-worker.exe

ingest: ## Ingestion directe en CLI (URL=... [LANGS=fr])
	@if [ -z "$(URL)" ]; then echo "Usage: make ingest URL=\"https://youtu.be/xxxx\" [LANGS=fr]"; exit 1; fi
	$(VENV)/toumai-ingest.exe "$(URL)" --lang $(LANGS)
