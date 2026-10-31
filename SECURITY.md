# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

---

## Reporting a Vulnerability

We take the security of Incident Copilot seriously. If you discover a security vulnerability or security bug, please report it privately:

1. **Do not open a public GitHub issue.**
2. Send an email to `nijjilnarula@gmail.com` with the subject line:  
   `[SECURITY VULNERABILITY] Incident Copilot - <Brief Description>`
3. Include detailed steps to reproduce the vulnerability, including:
   - Component affected (`api`, `ui`, `caddy`, `guardrail`, etc.).
   - Proof-of-concept payload or execution log.
   - Potential impact assessment.
4. You will receive an acknowledgment within 48 hours, followed by updates regarding remediation and patch release.

---

## Security Controls Implemented

Incident Copilot incorporates defense-in-depth architectural security controls:

### 1. Command Safety Guardrail
- LLM completions undergo whole-command matching against verified runbooks (`doc_type=runbook`).
- Hallucinated or unauthorized shell commands are automatically redacted to `[UNVERIFIED COMMAND REMOVED]`.
- Dangerous commands (`rm -rf`, `DROP DATABASE`, `TRUNCATE`, `mkfs`) trigger visual caution badges.
- Post-mortems are strictly grounded in factual chat transcripts and cannot verify runbook commands.

### 2. Isolated Network Perimeter (No Published Backend Ports)
- In production (`docker-compose.prod.yml`), only the Caddy reverse proxy publishes ports (`80` and `443`).
- `api`, `ui`, `postgres`, and `redis` have their host port mappings reset (`ports: !reset []`), isolating them entirely within the internal private Docker bridge network.

### 3. Non-Root Container Execution
- The `api` container executes under an unprivileged dedicated user (`appuser`, UID/GID 1000).
- Application processes do not have root privileges inside container namespaces.

### 4. Edge Authentication & HTTPS
- Caddy terminates TLS using automatic modern ACME certificates.
- All requests are gated behind HTTP Basic Authentication (`basic_auth`) before reaching application containers.
- Strict security headers (`HSTS`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`) and `-Server` banner removal are enforced.

### 5. Local Privacy & Embeddings
- Vector embeddings run entirely on-premise on CPU via SentenceTransformers; proprietary internal runbooks and architecture documents are never sent to third-party embedding APIs.
