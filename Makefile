.PHONY: up down logs rebuild

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=200

rebuild:
	docker compose build --no-cache


