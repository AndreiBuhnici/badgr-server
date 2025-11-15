#!/bin/bash
set -e

sleep 5 # wait a little bit more for the database to be fully ready

# 1. Run migrations
echo "Starting migrations"
python /badgr_server/manage.py migrate

# 2. Create superuser and oauth2 client
echo "Seed database for local development"
python /badgr_server/manage.py seed

# 3. Start app
echo "Starting app"
exec python /badgr_server/manage.py runserver 0.0.0.0:8000