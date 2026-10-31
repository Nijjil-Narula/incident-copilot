# Post-mortem: Incident 401 - Database Connection Pool Saturation

## Summary
On October 12, ledger-service exhausted available PostgreSQL connection pools, resulting in elevated 503 error rates across downstream transaction endpoints.

## Impact
During the 28-minute outage window, 14% of payment write requests failed with connection timeout errors. gateway-api successfully rejected surplus traffic using backpressure shedding.

## Timeline
- 14:02 UTC - Automated alerts trigger for ledger-service database connection saturation.
- 14:06 UTC - On-call engineer initiates incident triage and inspects postgres connection counts.
- 14:14 UTC - Root cause identified as runaway background reporting queries opening unbounded overflow connections.
- 14:22 UTC - Emergency configuration applied disabling connection pool overflow.
- 14:30 UTC - Service latency returns to baseline; incident marked as resolved.

## Root cause
A scheduled analytics export query invoked from reporting-service held open idle database transactions without releasing connections back to the pooler. Under peak traffic, ledger-service exhausted all remaining PostgreSQL backend process slots.

## Resolution
Engineers eliminated pool overflow capacity and capped maximum active client connections by applying an immediate configuration update:
```bash
kubectl set env deployment/ledger-service DB_POOL_MAX_OVERFLOW=0 -n payments
```

## Lessons
- Database connection pools must enforce strict upper bounds on overflow connections across all microservices.
- Long-running analytics queries must be physically isolated to read-only replicas to protect transactional workloads.
- Add connection pool saturation alerts at 75% capacity to provide early warning prior to hard exhaustion.
