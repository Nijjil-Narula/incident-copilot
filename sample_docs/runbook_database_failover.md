# Runbook: PostgreSQL Database Failover

## When to use
Use this runbook when the primary PostgreSQL database node becomes unresponsive, experiences unrecoverable hardware failure, or when an emergency failover to a healthy replica is required to restore transactions for ledger-service.

## Remediation Steps

1. Inspect cluster replication state and identify candidate replicas:
```bash
kubectl exec -it postgres-0 -n payments -- patronictl list
```

2. Execute manual failover to the designated standby replica:
```bash
kubectl exec -it postgres-0 -n payments -- patronictl failover --candidate postgres-1
```

3. Restart the core transaction engine to reset stale client connection pools:
```bash
kubectl rollout restart deployment/ledger-service -n payments
```

## Verification
Confirm that the standby node has assumed the leader role and that replication topology is healthy:
```bash
kubectl exec -it postgres-1 -n payments -- patronictl topology
```
Verify that ledger-service pods have reconnected without errors:
```bash
kubectl get pods -l app=ledger-service -n payments
```

## Escalation
If the failover command times out or patronictl reports split-brain state, immediately page the Database Administrator on-call and notify the Incident Commander on the active incident channel.
