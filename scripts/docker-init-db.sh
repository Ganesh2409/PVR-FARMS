#!/bin/sh
# Runs once when the Postgres container volume is first created: creates the dedicated app role + database.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<EOSQL
CREATE ROLE mango_app LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE DATABASE mango_farm OWNER mango_app ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE mango_farm FROM PUBLIC;
EOSQL
