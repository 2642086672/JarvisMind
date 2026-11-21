#!/bin/sh
# Start JarvisMind in the container.
#
# The database is created once, when it is missing. Existing databases are never
# initialized again and never migrated here: upgrades are explicit operator actions.
set -eu

DATA_DIR="${JARVISMIND_DATA_DIR:-/data}"

if [ ! -f "${DATA_DIR}/jarvismind.db" ]; then
    echo "JarvisMind: no database in ${DATA_DIR}; creating a new one"
    python -m jarvismind init-db
fi

exec python -m jarvismind serve
