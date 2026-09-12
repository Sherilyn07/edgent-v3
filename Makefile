# Two compose files, two targets each. `make <x>` operates on the local dev
# stack (docker-compose.yml — SQLite, local Redis, four containers). `make
# ec2-<x>` operates on what actually runs on an EC2 instance
# (docker-compose.ec2.yml — web + api only, against RDS/ElastiCache/ECS).
#
# There is no `make url` here the way version 2 had one: v3 has no tunnel.
# The public address is whatever CloudFront hands you — see DEPLOY.md §10.

.PHONY: up down clean build logs ps queues ready shell-db scale-ingest \
       ec2-up ec2-down ec2-build ec2-logs ec2-ps

# --- local dev (docker-compose.yml) ------------------------------------------

up:              ## build and start the local dev stack
	docker compose up -d --build

down:
	docker compose down

clean:           ## also delete the volumes — including the sqlite file
	docker compose down -v

build:
	docker compose build

logs:
	docker compose logs -f

ps:
	docker compose ps

queues:          ## how far behind are we
	@curl -s localhost:$${WEB_PORT:-8080}/api/metrics/queues | python3 -m json.tool

ready:           ## is every dependency reachable
	@curl -s localhost:$${WEB_PORT:-8080}/api/ready | python3 -m json.tool

scale-ingest:    ## more ingest workers, locally: make scale-ingest N=3
	docker compose up -d --scale ingest-worker=$(or $(N),3)

shell-db:        ## open the local sqlite file
	docker compose exec api python -c "import sqlite3,sys; \
	  [print(r) for r in sqlite3.connect('/data/edgentrag.db').execute( \
	  'select name from sqlite_master where type=\"table\"')]"

# --- the EC2 box (docker-compose.ec2.yml) ------------------------------------
# Run these ON the instance, not from your laptop. Scaling the API means
# changing the Auto Scaling Group's desired count (DEPLOY.md §7), not this
# file. Scaling the workers means changing the ECS services' desired count or
# their Application Auto Scaling targets (DEPLOY.md §6) — they are not
# containers here at all.

ec2-up:
	docker compose -f docker-compose.ec2.yml up -d --build

ec2-down:
	docker compose -f docker-compose.ec2.yml down

ec2-build:
	docker compose -f docker-compose.ec2.yml build

ec2-logs:
	docker compose -f docker-compose.ec2.yml logs -f

ec2-ps:
	docker compose -f docker-compose.ec2.yml ps
