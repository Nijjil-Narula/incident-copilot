# Post-mortem: Incident 403 - Kafka Topic Partition Skew

## Summary
On October 19, notification-worker experienced extreme consumer lag exceeding 180,000 messages on outbound notification topics due to partition key skew in Kafka.

## Impact
Customer notification delivery delayed by up to 45 minutes for transactions processed between 09:15 UTC and 10:00 UTC. Ingress transactions and database writes remained unaffected.

## Timeline
- 09:18 UTC - High consumer lag alarm fires for notification-workers-group.
- 09:24 UTC - SRE investigates consumer group metrics and detects partition 3 processing backlog.
- 09:35 UTC - Root cause confirmed: bulk merchant transaction events sharing a single partition key.
- 09:44 UTC - Partition reassignment plan executed across Kafka brokers.
- 10:03 UTC - Consumer lag drains below 1,000 messages; notification latency normalizes.

## Root cause
ledger-service published events keyed exclusively by merchant ID rather than composite event IDs. A single large merchant generated 82% of batch event traffic, causing partition 3 to saturate a single worker while remaining workers sat idle.

## Resolution
Engineers rebalanced partition leadership across available cluster brokers using an offline reassignment manifest:
```bash
kafka-reassign-partitions.sh --bootstrap-server kafka:9092 --reassignment-json-file /tmp/rebalance.json --execute
```

## Lessons
- Event partitioning keys must incorporate random salt or sub-account IDs to avoid hot partition skew.
- Add alerting for single-partition lag variance across consumer group worker threads.
- Implement rate limiting on bulk event publishing from ledger-service.
