# TOUMAI — raccourcis de dev. À lancer depuis Git Bash.
#   make            -> aide
#   make install    -> venv + dépendances
#
#   Dev sur l'hôte (app en local, infra en conteneurs) :
#     make up         -> infra seule (postgres + minio + kafka)
#     make api        -> API FastAPI    (terminal 1)
#     make worker     -> worker Kafka   (terminal 2)
#     make ingest URL="https://youtu.be/xxxx" LANGS=fr
#     make veille     -> une passe de veille
#
#   Tout en conteneurs (API + worker + infra) :
#     make stack-up   -> build + démarre toute la stack
#     make stack-logs -> suit les logs api + worker
#     make scale N=3  -> N workers
#     make stack-down -> arrête la stack

SHELL := bash
VENV := .venv/Scripts
PY := $(VENV)/python.exe
COMPOSE := docker compose

# services d'infra seuls (pour le workflow dev où api/worker tournent sur l'hôte)
INFRA := postgres minio kafka createbuckets

LANGS ?= fr
N ?= 3

.DEFAULT_GOAL := help
.PHONY: help install test lint fmt up down ps logs api worker ingest veille \
        build stack-up stack-down stack-logs scale clean

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Environnement --------------------------------------------------------
install: ## Crée le venv et installe le projet (+ dev)
	python -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

test: ## Lance les tests
	$(PY) -m pytest -q -vvv

lint: ## Vérifie le style (ruff + black --check)
	$(PY) -m ruff check src tests
	$(PY) -m black --check src tests

fmt: ## Formate le code (black + ruff --fix)
	$(PY) -m black src tests
	$(PY) -m ruff check --fix src tests

# --- Infra Docker (dev sur l'hôte) ----------------------------------------
up: ## Démarre l'infra seule (postgres + minio + kafka)
	$(COMPOSE) up -d $(INFRA)

down: ## Arrête l'infra (et la stack si lancée)
	$(COMPOSE) down

ps: ## État des conteneurs
	$(COMPOSE) ps

logs: ## Suit les logs de l'infra
	$(COMPOSE) logs -f $(INFRA)

clean: ## Arrête tout et supprime les volumes (⚠ données)
	$(COMPOSE) down -v

# --- Application sur l'hôte ------------------------------------------------
api: ## Lance l'API FastAPI (http://localhost:8000/docs)
	$(VENV)/toumai-api.exe

worker: ## Lance un worker Kafka
	$(VENV)/toumai-worker.exe

ingest: ## Ingestion directe en CLI (URL=... [LANGS=fr])
	@if [ -z "$(URL)" ]; then echo "Usage: make ingest URL=\"https://youtu.be/xxxx\" [LANGS=fr]"; exit 1; fi
	$(VENV)/toumai-ingest.exe "$(URL)" --lang $(LANGS)

veille: ## Lance une passe de veille (chaînes surveillées)
	$(VENV)/toumai-veille.exe

# --- Stack complète en conteneurs -----------------------------------------
build: ## Build l'image applicative (toumai-app)
	$(COMPOSE) build

stack-up: ## Build + démarre toute la stack (infra + api + worker)
	$(COMPOSE) up -d --build

stack-down: ## Arrête la stack
	$(COMPOSE) down

stack-logs: ## Suit les logs de l'API et du worker
	$(COMPOSE) logs -f toumai-api toumai-worker

scale: ## Scale les workers en conteneurs (N=3)
	$(COMPOSE) up -d --scale toumai-worker=$(N) toumai-worker
