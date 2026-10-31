#!/usr/bin/env bash
# scripts/restore-drill.sh
# Automated Disaster Recovery Restore Verification Drill for Incident Copilot
# Restores the newest local backup archive into an ephemeral scratch database,
# validates row counts across primary tables, and destroys the scratch DB.
#
# Live production database is NEVER modified or overwritten.
#
# Usage:
#   ./scripts/restore-drill.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${PROJECT_ROOT}/backups"
ENV_FILE="${PROJECT_ROOT}/.env"

# ── 1. Validate Environment & Credentials ──────────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    echo "ERROR: .env file not found at ${ENV_FILE}" >&2
    exit 1
fi

POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"'"'"' || true)"
POSTGRES_DB="$(grep -E '^POSTGRES_DB=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"'"'"' || true)"

if [ -z "${POSTGRES_USER}" ] || [ -z "${POSTGRES_DB}" ]; then
    echo "ERROR: Failed to extract POSTGRES_USER or POSTGRES_DB from .env" >&2
    exit 1
fi

# Locate the newest backup archive
LATEST_BACKUP="$(find "${BACKUP_DIR}" -maxdepth 1 -name "incident-copilot-*.sql.gz" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2- || true)"

if [ -z "${LATEST_BACKUP}" ] || [ ! -f "${LATEST_BACKUP}" ]; then
    echo "ERROR: No backup archives found in ${BACKUP_DIR}. Run scripts/backup.sh first." >&2
    exit 1
fi

echo "=========================================================="
echo " Starting Disaster Recovery Restore Drill                 "
echo " Target Backup: ${LATEST_BACKUP}"
echo "=========================================================="

cd "${PROJECT_ROOT}"

# Generate unique ephemeral scratch database name
RANDOM_SUFFIX="$(date +%s)_$$"
SCRATCH_DB="restore_drill_scratch_${RANDOM_SUFFIX}"

# ── 2. Register Cleanup Trap for Guaranteed Scratch DB Deletion ────────────
cleanup() {
    local exit_code=$?
    echo "--> Cleaning up scratch database: ${SCRATCH_DB}..."
    docker compose exec -T postgres psql -U "${POSTGRES_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1 || true
    if [ "${exit_code}" -eq 0 ]; then
        echo "--> Restore drill PASSED cleanly."
    else
        echo "--> Restore drill FAILED with exit code ${exit_code}." >&2
    fi
    exit "${exit_code}"
}
trap cleanup EXIT INT TERM

# ── 3. Create Scratch Database ─────────────────────────────────────────────
echo "--> Creating ephemeral scratch database: ${SCRATCH_DB}..."
docker compose exec -T postgres psql -U "${POSTGRES_USER}" -d postgres -c "CREATE DATABASE ${SCRATCH_DB};"

# ── 4. Restore Backup Into Scratch Database ────────────────────────────────
echo "--> Restoring gzip archive into ${SCRATCH_DB}..."
gunzip -c "${LATEST_BACKUP}" | docker compose exec -T postgres psql -U "${POSTGRES_USER}" -d "${SCRATCH_DB}" >/dev/null

# ── 5. Validate Table Row Counts ───────────────────────────────────────────
echo "--> Verifying restored database row counts..."

query_count() {
    local table="$1"
    docker compose exec -T postgres psql -U "${POSTGRES_USER}" -d "${SCRATCH_DB}" -t -A -c "SELECT COUNT(*) FROM ${table};" 2>/dev/null || echo "0"
}

INCIDENTS_COUNT="$(query_count "incidents")"
MESSAGES_COUNT="$(query_count "messages")"
DOCUMENTS_COUNT="$(query_count "documents")"
CHUNKS_COUNT="$(query_count "chunks")"

echo "----------------------------------------------------------"
echo " Restored Table Metrics:"
echo "   - incidents: ${INCIDENTS_COUNT} rows"
echo "   - messages:  ${MESSAGES_COUNT} rows"
echo "   - documents: ${DOCUMENTS_COUNT} rows"
echo "   - chunks:    ${CHUNKS_COUNT} rows"
echo "----------------------------------------------------------"

# Enforce non-zero incident count assertion
if [ "${INCIDENTS_COUNT}" -eq 0 ]; then
    echo "ERROR: Assertion failed! Restored 'incidents' table contains 0 rows." >&2
    exit 2
fi

echo "--> Data integrity assertion satisfied (incidents > 0)."
