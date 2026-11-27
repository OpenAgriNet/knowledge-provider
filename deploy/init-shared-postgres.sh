#!/bin/sh
set -eu

# Connect to the shared server over the private Compose network. This job can
# resume after a partial run, without replacing existing databases or roles.
psql --no-psqlrc --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
WITH roles(name) AS (VALUES ('knowledge_provider'), ('temporal'), ('keycloak'))
SELECT format('CREATE ROLE %I LOGIN', roles.name)
FROM roles WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = roles.name)
\gexec

WITH databases(name, owner) AS (
    VALUES ('knowledge_provider', 'knowledge_provider'),
           ('temporal', 'temporal'),
           ('temporal_visibility', 'temporal'),
           ('keycloak', 'keycloak')
)
SELECT format('CREATE DATABASE %I OWNER %I', databases.name, databases.owner)
FROM databases WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = databases.name)
\gexec

REVOKE CONNECT ON DATABASE knowledge_provider FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
REVOKE CONNECT ON DATABASE keycloak FROM PUBLIC;
SQL

# psql encrypts the passwords locally. Keep plaintext out of SQL and argv.
set_role_credential() {
    printf '%s\n%s\n' "$2" "$2" |
        psql --no-psqlrc --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres --command="\\password $1"
}

set_role_credential knowledge_provider "$KP_DB_PASSWORD"
set_role_credential temporal "$TEMPORAL_DB_PASSWORD"
set_role_credential keycloak "$KEYCLOAK_DB_PASSWORD"
