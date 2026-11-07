.PHONY: up down logs test

up:
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f api

test:
	docker compose --profile test run --rm --build test
