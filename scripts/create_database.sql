-- Run ONCE as the PostgreSQL superuser (postgres) - it is only used for this one-time setup.
-- Replace CHANGE_ME_STRONG_PASSWORD first. The application then connects as mango_app, which can NOT
-- create roles/databases or read other databases.
--
--   psql -U postgres -h localhost -v ON_ERROR_STOP=1 -f scripts\create_database.sql
--
CREATE ROLE mango_app LOGIN PASSWORD 'Ganesh24@@'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

CREATE DATABASE mango_farm      OWNER mango_app ENCODING 'UTF8' TEMPLATE template0;
CREATE DATABASE mango_farm_test OWNER mango_app ENCODING 'UTF8' TEMPLATE template0;

REVOKE ALL ON DATABASE mango_farm      FROM PUBLIC;
REVOKE ALL ON DATABASE mango_farm_test FROM PUBLIC;
