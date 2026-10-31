# On-Call Engineer Handbook: Severity Levels and Escalation

## Purpose
This handbook outlines incident severity classifications, operational response SLAs, and escalation pathways for on-call engineers supporting the platform in the payments namespace.

## Severity Level Definitions

### SEV-1 (Critical)
- **Definition**: Critical production outage causing complete failure of payment processing or data corruption.
- **Affected Systems**: Complete outage of gateway-api or ledger-service, or primary postgres failure.
- **Target Response**: Page acknowledgment within 5 minutes. Initial customer status update within 15 minutes.
- **Action**: Immediately declare an incident, open a dedicated coordination bridge, and page the Incident Commander.

### SEV-2 (High)
- **Definition**: Major functionality severely degraded or significant latency increase impacting customers.
- **Affected Systems**: Intermittent errors in auth-service, or severe consumer lag in notification-worker.
- **Target Response**: Page acknowledgment within 15 minutes. Mitigation plan within 45 minutes.

### SEV-3 (Medium)
- **Definition**: Minor system degradation or partial loss of non-essential features with an existing workaround.
- **Affected Systems**: Delayed batch aggregations in reporting-service, or elevated cache miss rates on redis.
- **Target Response**: Acknowledgment within 1 hour during business hours.

### SEV-4 (Low)
- **Definition**: Minor inconvenience, non-critical log volume anomalies, or cosmetic dashboard errors.
- **Affected Systems**: Background report formatting errors or internal metric collection delays.
- **Target Response**: Triage within 2 business days.

## Escalation Paths
1. If the primary on-call engineer does not acknowledge a SEV-1 or SEV-2 page within 10 minutes, the paging system automatically escalates to the secondary on-call engineer.
2. If a SEV-1 remains unresolved after 45 minutes, the Incident Commander must escalate directly to the Engineering Director.
3. For database corruption or split-brain scenarios, immediately page the dedicated Database Administrator on-call.

## Incident Commands
Verify active pod and event status during triage:
```bash
kubectl get events -n payments --sort-by=.metadata.creationTimestamp
```
