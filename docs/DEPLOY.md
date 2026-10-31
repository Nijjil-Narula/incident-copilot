# Oracle Cloud ARM64 Deployment Guide

Complete operational guide for deploying **Incident Copilot** on a zero-cost, self-hosted Oracle Cloud Infrastructure (OCI) Always Free ARM64 virtual machine running Ubuntu 24.04 LTS.

---

## 1. Oracle Always Free Infrastructure Setup

### 1.1 Compute Instance Specifications
1. Log in to the [Oracle Cloud Console](https://cloud.oracle.com/).
2. Navigate to **Compute** > **Instances** > **Create Instance**.
3. Configure the following parameters:
   - **Image**: Ubuntu 24.04 Canonical (ARM64 / aarch64)
   - **Shape**: `VM.Standard.A1.Flex` (Ampere Altra ARM)
   - **OCPUs**: `2` OCPU
   - **Memory**: `12` GB RAM
   - **Boot Volume**: `50` GB (well within the 200 GB Always Free tier allowance)
   - **SSH Keys**: Upload your public SSH key.

### 1.2 OCI Security List (Virtual Cloud Network Ingress)
By default, OCI VCN security lists block inbound public traffic. You must explicitly open ports 80 and 443:
1. Navigate to **Networking** > **Virtual Cloud Networks** > Select your VCN > **Security Lists** > **Default Security List**.
2. Click **Add Ingress Rules**:
   - **Rule 1 (HTTP for ACME challenges)**:
     - Source CIDR: `0.0.0.0/0`
     - IP Protocol: `TCP`
     - Destination Port Range: `80`
     - Description: `HTTP Ingress for Caddy ACME challenges`
   - **Rule 2 (HTTPS for Application Access)**:
     - Source CIDR: `0.0.0.0/0`
     - IP Protocol: `TCP`
     - Destination Port Range: `443`
     - Description: `HTTPS Ingress for Incident Copilot`

### 1.3 Free Domain Name Setup (DuckDNS)
If you do not own a custom domain name, obtain a free subdomain from [DuckDNS](https://www.duckdns.org/):
1. Sign in to DuckDNS and create a domain (e.g. `my-incident-copilot.duckdns.org`).
2. Point the A record to the Public IP address of your Oracle Cloud instance.

---

## 2. Server Provisioning & First Deploy

### 2.1 Run Automated Host Provisioning
SSH into the instance as the default `ubuntu` user and clone the repository:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/Nijjil-Narula/incident-copilot.git /opt/incident-copilot
cd /opt/incident-copilot

# Execute automated host provisioning (Docker, swap, firewall, deploy user)
sudo ./scripts/server-setup.sh
```

### 2.2 Configure Production Environment (.env)
Switch to the non-root `deploy` user and configure secrets:

```bash
sudo su - deploy
cd /opt/incident-copilot
cp .env.example .env
chmod 600 .env
```

Generate the HTTP Basic Auth hash for Caddy using the official container image:

```bash
docker run --rm caddy:2.8.4-alpine caddy hash-password
# Enter your secure password when prompted
```

Edit `/opt/incident-copilot/.env`:
```ini
DOMAIN=my-incident-copilot.duckdns.org
BASIC_AUTH_USER=admin
# IMPORTANT: Wrap the bcrypt hash in single quotes to prevent Compose from interpolating "$" symbols
BASIC_AUTH_HASH='$2a$14$...'
GEMINI_API_KEY=your_gemini_api_key_here
POSTGRES_PASSWORD=your_strong_db_password_here
```

### 2.3 Validate Before Deploying
Run these verification commands locally or on the server before starting containers to validate syntax and configuration.

Generate a valid test hash first (Caddy base64-decodes the bcrypt hash during configuration validation, so a placeholder like `x` will fail):

```bash
TEST_HASH=$(docker run --rm caddy:2.8.4-alpine caddy hash-password --plaintext "throwaway-test-pw")
docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -e DOMAIN=localhost -e BASIC_AUTH_USER=admin -e BASIC_AUTH_HASH="$TEST_HASH" caddy:2.8.4-alpine caddy validate --config /etc/caddy/Caddyfile
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```

> **NOTE**: If Caddy validation returns an `illegal base64 data` error, it means the hash is missing, malformed, or had its `$` characters mangled by shell or compose interpolation. Ensure the hash in `.env` is wrapped in single quotes.

### 2.4 Start the Production Stack
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Verify service status:
```bash
docker compose ps
docker compose logs -f caddy
```

---

## 3. Maintenance, Updates & Rollbacks

### 3.1 Applying Updates
```bash
cd /opt/incident-copilot
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose ps
```

### 3.2 Rollback Procedure
If an issue occurs after an update, revert immediately to the previous Git tag or commit:
```bash
cd /opt/incident-copilot
git checkout <previous-tag-or-commit>
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

---

## 4. Backups, Restore Drills & Automated Monitoring

### 4.1 Automated Cron Configuration
Install cron jobs for nightly database backups and hourly disk capacity checks under the `deploy` user crontab (`crontab -e`):

```cron
# Nightly database backup at 02:00 UTC (keeps latest 7 archives)
0 2 * * * /opt/incident-copilot/scripts/backup.sh >> /var/log/incident-copilot-backup.log 2>&1

# Hourly disk usage monitoring & Docker storage check
0 * * * * /opt/incident-copilot/scripts/disk-check.sh >> /var/log/incident-copilot-disk.log 2>&1
```

> **IMPORTANT**: Always execute the disaster recovery restore drill at least once on the production VM before trusting backups:
> ```bash
> /opt/incident-copilot/scripts/restore-drill.sh
> ```

### 4.2 External Uptime Monitoring
Configure a free external uptime monitoring service (such as UptimeRobot or Better Uptime) targeting `https://<YOUR-DOMAIN>/_stcore/health` or `https://<YOUR-DOMAIN>/health`.

Because Caddy enforces HTTP Basic Authentication globally across the domain, external monitors will receive `401 Unauthorized` unless accommodated. Options to consider:
1. **Authenticated Monitor (Recommended)**: Configure the external monitor to send the `Authorization: Basic <base64-credentials>` header with the request.
2. **Dedicated Public Health Endpoint**: Modify the `Caddyfile` to route a specific health path (e.g. `/health/public`) without requiring basic authentication.
3. **Internal Host Healthcheck**: Monitor container health locally using systemd or Docker daemon healthcheck events.

---

## 5. Cost Avoidance & Oracle Cloud Free Tier Rules

### 5.1 Strict Zero-Cost Rules
To guarantee zero cloud charges:
1. **Never upgrade to "Pay As You Go"**: Keep your account on the Free Tier status.
2. **Instance Limits**: Never exceed 4 OCPUs or 24 GB RAM across all Ampere A1 instances in your tenancy (our VM uses 2 OCPUs and 12 GB RAM, leaving half your allowance free).
3. **Storage Limits**: Total boot and block volume storage across your tenancy must remain under 200 GB (our instance uses 50 GB).
4. **Set Budget Alerts**: In OCI Console, navigate to **Billing & Cost Management** > **Budgets** > Create a monthly budget of $1.00 with email alerts at 50% and 100%.

### 5.2 OCI Idle Resource Reclaim Policy
Oracle periodically reclaims idle Always Free compute instances. Review Oracle's official documentation on idle instance reclamation criteria:
- [Oracle Cloud Free Tier Documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

Instances are considered idle if during a 7-day period:
- CPU utilization is less than 20% (for the 95th percentile).
- Network utilization is less than 20%.
- Memory utilization is less than 20% (applicable to A1 shapes).

Running automated nightly backups, vector search triage, and periodic healthchecks naturally keeps the instance active.

---

## 6. What is NOT Production-Ready Yet

The following items are functional for internal team evaluation but require hardening before enterprise production deployment:
1. **No Authentication Layer**: Uses shared HTTP Basic Auth through Caddy with no user accounts, role-based access control (RBAC), or audit logging of user actions.
2. **Single Uvicorn Worker**: In-memory FAISS indices, single-flight answer locks, and the in-progress resolution concurrency guard rely on a single process.
3. **No Database Migrations**: Relies on SQLAlchemy `create_all` rather than versioned Alembic migrations.
4. **Global Answer Cache**: Cache keys are derived from questions and documents, not isolated per tenant or user.
5. **No Chat History in Context**: Prompts send only the current question and retrieved runbook chunks; prior multi-turn dialog is not fed to the LLM.
6. **Command Suitability Limitations**: The safety guardrail verifies that a command appears in a verified runbook, but cannot guarantee that the command is appropriate for the incident situation.
7. **No PII/Data Scrubbing**: Prompts and retrieved document chunks pass directly to the Gemini API; sensitive internal text is not redacted.
8. **No Document Retention/Deletion API**: No automated data retention policies or bulk purge endpoints exist.
9. **Global Rate Limits**: No per-user or per-IP throttling beyond basic gateway limits.
