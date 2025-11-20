#!/bin/bash

# 1. Run migrations
echo "Starting migrations"
docker-compose exec api python /badgr_server/manage.py migrate

# 2. Create swagger docs
echo "Create swagger docs"
docker-compose exec api python /badgr_server/manage.py dist

# 3. Create superuser and oauth2 client
echo "Seed database for local development"
docker-compose exec api python /badgr_server/manage.py seed