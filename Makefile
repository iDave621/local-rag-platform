.PHONY: init build prepare up down logs db-init admin backup
init:
	bash server/scripts/init-config.sh
build:
	docker compose build api frontend
prepare:
	bash server/scripts/prepare-offline-images.sh
up:
	bash server/scripts/start-local.sh
down:
	docker compose down
logs:
	docker compose logs -f --tail=200
db-init:
	docker compose exec api python -m app.cli init-db
admin:
	docker compose exec api python -m app.cli create-user --role admin
backup:
	bash server/scripts/backup.sh
