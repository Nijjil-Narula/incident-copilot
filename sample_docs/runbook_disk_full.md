# Runbook: Disk Full Remediation on Database and Worker Nodes

## When to use
Use this runbook when disk space on persistent storage volumes for reporting-service or postgres reaches 85% capacity, threatening service termination due to full filesystems.

## Remediation Steps

1. Inspect current disk space utilization on the local mount:
```bash
df -h /var/log/reporting-service
```

2. List old compressed log archives eligible for cleanup:
```bash
find /var/log/reporting-service -name "*.gz" -mtime +3
```

3. Purge archived log files to reclaim capacity:
**LAST RESORT**
```bash
find /var/log/reporting-service -name "*.gz" -mtime +3 -delete
```

4. If postgres storage is critically full and blocking transaction logs:
**LAST RESORT**
```bash
psql -h postgres -d postgres -U postgres -c "TRUNCATE TABLE audit_log_staging;"
```

## Verification
Confirm that filesystem utilization has dropped below 70%:
```bash
df -h /var/log/reporting-service
```
Verify reporting-service health status:
```bash
kubectl get pods -l app=reporting-service -n payments
```

## Escalation
If disk capacity remains above 80% after executing cleanup procedures, escalate immediately to the Infrastructure Storage team to dynamically expand volume size.
