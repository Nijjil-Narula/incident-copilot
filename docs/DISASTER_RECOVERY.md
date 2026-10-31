# Disaster Recovery Playbook

Procedures for recovering **Incident Copilot** from catastrophic host failure, data corruption, or regional cloud outages.

---

## 1. Objectives & Metrics

- **Recovery Point Objective (RPO)**: The timestamp of the most recent nightly backup archive (at most 24 hours of data loss under automated schedule).
- **Recovery Time Objective (RTO)**: 
  - **Target Goal**: `< 15 minutes` to restore service on a newly provisioned virtual machine.
  - **Actual Measured Time (from first drill)**: `_____ minutes`

---

## 2. Recovery Architecture & State Mechanics

Incident Copilot maintains two primary persistent stores:
1. **PostgreSQL 16**: Authoritative source of record for all incidents, messages, document metadata, and text chunks.
2. **FAISS Vector Index**: Ephemeral search index persisted to disk.

> **CRITICAL ARCHITECTURAL GUARANTEE**:
> When the `api` container boots, if the local FAISS index on disk is missing or corrupted, the service automatically executes `rebuild_from_db()`: it reads all chunk embeddings and texts directly from PostgreSQL and rebuilds the FAISS vector index in memory.
>
> Therefore, **restoring PostgreSQL from the latest backup archive completely recovers both relational ticket history and vector search capabilities.**

---

## 3. Fresh VM Reconstruction Steps

Follow these numbered steps to restore the stack on a fresh Ubuntu 24.04 instance:

### Step 1: Provision Host Infrastructure
Provision an Ubuntu 24.04 VM (ARM64 or x86_64) and configure firewall rules according to [docs/DEPLOY.md](DEPLOY.md).

### Step 2: Clone Repository & Run Provisioning Script
```bash
sudo git clone https://github.com/Nijjil-Narula/incident-copilot.git /opt/incident-copilot
cd /opt/incident-copilot
sudo ./scripts/server-setup.sh
```

### Step 3: Retrieve Secrets & Latest Backup File
Switch to the `deploy` user and restore the production `.env` and latest database backup archive:
```bash
sudo su - deploy
cd /opt/incident-copilot

# Restore .env file from secure off-site password vault
cp /path/to/secured/.env .env
chmod 600 .env

# Create backups directory and place the target archive
mkdir -p backups
# Example: retrieve from off-site rclone remote or secure storage
# rclone copy "myremote:backups/incident-copilot-LATEST.sql.gz" backups/
```

### Step 4: Boot Infrastructure Containers
Start only the PostgreSQL and Redis containers first to prepare for data restoration:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d postgres redis
```

Wait until PostgreSQL reports healthy status:
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

### Step 5: Restore Database Archive
Restore the gzipped SQL archive into PostgreSQL:
```bash
LATEST_BACKUP="$(find backups -name "incident-copilot-*.sql.gz" | sort -r | head -n 1)"
gunzip -c "${LATEST_BACKUP}" | docker compose exec -T postgres psql -U copilot_user -d incident_copilot
```

### Step 6: Boot Application & Reverse Proxy
Launch the remaining containers (`api`, `ui`, `caddy`):
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Step 7: Verification & Health Check
1. Verify all five services are healthy:
   ```bash
   docker compose ps
   ```
2. Verify that FAISS has synchronized from PostgreSQL chunks:
   ```bash
   docker compose logs api | grep -i "faiss"
   ```
3. Execute the automated disaster recovery drill script to ensure structural integrity:
   ```bash
   ./scripts/restore-drill.sh
   ```
4. Access the web interface at `https://<YOUR-DOMAIN>` and perform a test triage search.
