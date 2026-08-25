.PHONY: build up down logs restart ps

build:
	docker compose build

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api

restart:
	docker compose restart api

ps:
	docker compose ps
