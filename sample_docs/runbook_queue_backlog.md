# Runbook: Kafka Queue Consumer Lag and Backlog

## When to use
Use this runbook when consumer lag on Kafka notification topics exceeds 50,000 messages or notification-worker processing delays impact customer notifications.

## Remediation Steps

1. Inspect consumer group lag across all topic partitions:
```bash
kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group notification-workers-group
```

2. Scale out notification-worker deployment replicas to increase consumption rate:
```bash
kubectl scale deployment/notification-worker --replicas=10 -n payments
```

3. If consumer worker threads are locked, trigger a rolling deployment restart:
```bash
kubectl rollout restart deployment/notification-worker -n payments
```

## Verification
Re-run consumer group inspection to confirm lag is actively decreasing towards zero:
```bash
kafka-consumer-groups.sh --bootstrap-server kafka:9092 --describe --group notification-workers-group
```
Verify notification-worker pod health in the payments namespace:
```bash
kubectl get pods -l app=notification-worker -n payments
```

## Escalation
If consumer lag continues to increase despite scaling, escalate to the Distributed Systems on-call team to inspect partition skew and broker partition leadership.
