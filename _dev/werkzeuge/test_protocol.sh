#!/usr/bin/env bash
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
#
# Test protocol of a release (spec 9.9, § 87c (3) AO).
#
# Runs the tests of the modules in a fresh database of the local docker
# compose setup (service "odoo", database service "db") with coverage of lines
# and branches and writes into the output directory:
#
#   junit.xml       result of every test (JUnit XML, one test suite per module)
#   coverage.xml    coverage per line and branch (Cobertura XML)
#   coverage.json   the same as JSON, coverage.txt the summary per file
#   coverage-html/  coverage per line to read in the browser
#   test.log        log of the run
#   summary.md      tag, commit, Odoo and PostgreSQL versions, image, date,
#                   test counts and coverage per module (protocol_summary.py;
#                   a failed setUpClass counts for its module, results
#                   outside the modules appear as "other")
#
# With modules for a second run (-x, default EXTRA below), the tests run a
# second time in a database of its own in which these modules are installed
# first; junit-2.xml and runs.json describe that run, and the coverage is the
# combined one of both runs.
#
# The Odoo image stays unchanged: coverage is installed into the throw-away
# container (pip --target /tmp) and the database is dropped at the end.
#
# Usage (from anywhere inside the repository):
#   _dev/werkzeuge/test_protocol.sh [-d DATABASE] [-o OUTPUT_DIR]
#                                   [-m MODULE,MODULE,...] [-x MODULE,...] [-k]
#   -d  database to create (must not exist; default: protocol_<timestamp>);
#       the second run uses DATABASE_2
#   -o  output directory (default: build/test-protocol-<version>-<timestamp>)
#   -m  modules to install and test (default: the module of the repo)
#   -x  modules of the second run (default: EXTRA below; empty: one run)
#   -k  keep the databases after the run
#
# Exit status: 0 if all tests passed, 1 if a test failed or Odoo stopped
# with an error, 2 on wrong usage or a database that already exists.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
DB="protocol_${STAMP//-/_}"
MODULES="l10n_de_euer"
OUT=""
KEEP=0
COVERAGE_VERSION="7.16.2"
# second run with the OCA reconciliation modules (spec 9.9 "Matrix
# OCA-Abgleich"), as in the job "test with OCA reconciliation modules"
EXTRA="account_reconcile_oca,account_in_payment,account_payment_order"
EXTRA_LABEL="with OCA reconciliation modules"

while getopts "d:o:m:x:kh" option; do
    case "$option" in
        d) DB="$OPTARG" ;;
        o) OUT="$OPTARG" ;;
        m) MODULES="$OPTARG" ;;
        x) EXTRA="$OPTARG" ;;
        k) KEEP=1 ;;
        *) sed -n '4,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
done

cd "$REPO"
VERSION="$(python3 -c "import ast; print(ast.literal_eval(open('${MODULES%%,*}/__manifest__.py').read())['version'])")"
OUT="${OUT:-build/test-protocol-${VERSION}-${STAMP}}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
TAGS="$(echo "$MODULES" | sed 's/[^,]*/\/&/g')"

db_exists() {
    docker compose exec -T db psql -U odoo -d postgres -Atc \
        "SELECT 1 FROM pg_database WHERE datname = '$1'" | grep -q 1
}
for name in "$DB" ${EXTRA:+"${DB}_2"}; do
    if db_exists "$name"; then
        echo "Database $name exists already; choose another one with -d." >&2
        exit 2
    fi
done

echo "Test protocol $VERSION: database $DB, modules $MODULES${EXTRA:+, second run in ${DB}_2 with $EXTRA}"
echo "Output: $OUT (takes about 30 to 60 minutes)"

# runs inside the container: tests with coverage, then the reports; the
# results leave the container as a tar stream on stdout, the log on stderr
read -r -d '' INNER <<'EOF' || true
set -e
exec 3>&1 1>&2
mkdir -p /tmp/protocol
cat > /tmp/protocol/coveragerc <<RC
[run]
branch = True
source = $(echo "$MODULES" | sed 's/[^,]*/\/mnt\/extra-addons\/&/g')
omit =
    */tests/*
    */__manifest__.py
    */__init__.py
data_file = /tmp/protocol/.coverage

[report]
precision = 2
skip_empty = True

[html]
directory = /tmp/protocol/coverage-html
RC
pip install --quiet --target /tmp/cov "coverage==$COVERAGE_VERSION" > /dev/null 2>&1
export PYTHONPATH=/tmp/cov
status=0
python3 -m coverage run --rcfile=/tmp/protocol/coveragerc \
    --data-file=/tmp/protocol/.coverage.1 \
    /mnt/extra-addons/_dev/werkzeuge/odoo_junit.py \
    --junit-xml /tmp/protocol/junit.xml \
    -c /etc/odoo/odoo.conf -d "$DB" -i "$MODULES" --test-tags "$TAGS" \
    --stop-after-init --http-port=8099 || status=$?
if [ -n "$EXTRA" ]; then
    extra=0
    # the modules of the second run first, then the tests as in the first run
    odoo -c /etc/odoo/odoo.conf -d "${DB}_2" -i "$EXTRA" --stop-after-init \
        --no-http || extra=$?
    if [ "$extra" -eq 0 ]; then
        python3 -m coverage run --rcfile=/tmp/protocol/coveragerc \
            --data-file=/tmp/protocol/.coverage.2 \
            /mnt/extra-addons/_dev/werkzeuge/odoo_junit.py \
            --junit-xml /tmp/protocol/junit-2.xml \
            -c /etc/odoo/odoo.conf -d "${DB}_2" -i "$MODULES" --test-tags "$TAGS" \
            --stop-after-init --http-port=8099 || extra=$?
    fi
    printf '[{"label": "standard", "junit": "junit.xml", "database": "%s", "status": "%s"}, {"label": "%s (%s)", "junit": "junit-2.xml", "database": "%s_2", "status": "%s"}]\n' \
        "$DB" "$status" "$EXTRA_LABEL" "$EXTRA" "$DB" "$extra" > /tmp/protocol/runs.json
    if [ "$status" -eq 0 ]; then
        status=$extra
    fi
fi
cd /tmp/protocol
python3 -m coverage combine --rcfile=coveragerc .coverage.* > /dev/null || true
python3 -m coverage report --rcfile=coveragerc > coverage.txt || true
python3 -m coverage xml --rcfile=coveragerc -o coverage.xml > /dev/null || true
python3 -m coverage json --rcfile=coveragerc -o coverage.json > /dev/null || true
python3 -m coverage html --rcfile=coveragerc > /dev/null || true
echo "$status" > odoo_status
python3 -c "import odoo.release as r; print(r.version)" > odoo_version 2>/dev/null || true
rm -f .coverage
tar -C /tmp/protocol -cf - . >&3
EOF

docker compose run --rm --no-deps -T \
    -e DB="$DB" -e MODULES="$MODULES" -e TAGS="$TAGS" \
    -e COVERAGE_VERSION="$COVERAGE_VERSION" \
    -e EXTRA="$EXTRA" -e EXTRA_LABEL="$EXTRA_LABEL" \
    --entrypoint bash odoo -c "$INNER" \
    2> "$OUT/test.log" | tar -C "$OUT" -xf -

docker compose exec -T db psql -U odoo -d postgres -Atc "SHOW server_version" \
    > "$OUT/postgres_version" 2> /dev/null || true
IMAGE="$(docker compose config --format json 2> /dev/null | python3 -c \
    'import json, sys; print(json.load(sys.stdin)["services"]["odoo"]["image"])' \
    2> /dev/null || true)"
docker image inspect "$IMAGE" \
    --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' \
    > "$OUT/odoo_image" 2> /dev/null || true

if [ "$KEEP" -eq 0 ]; then
    for name in "$DB" ${EXTRA:+"${DB}_2"}; do
        docker compose exec -T db dropdb -U odoo --force --if-exists "$name" > /dev/null
    done
fi

python3 "$REPO/_dev/werkzeuge/protocol_summary.py" "$OUT" --repo "$REPO" \
    --modules "$MODULES" --database "$DB"
