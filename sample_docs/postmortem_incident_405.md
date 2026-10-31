# Post-mortem: Incident 405 - Ingress Gateway Request Timeout Storm

## Summary
On October 27, gateway-api suffered ingress socket exhaustion and widespread 504 Gateway Timeout errors caused by cascading slow queries in reporting-service.

## Impact
Gateway HTTP latency degraded from 45ms to over 8,000ms for 34 minutes. Ingress connection pool exhaustion triggered brief intermittent 502 errors across all public endpoints.

## Timeline
- 11:05 UTC - Ingress latency alert triggers for gateway-api with p99 exceeding 10 seconds.
- 11:12 UTC - SRE on-call identifies hundreds of open connections held by reporting-service queries.
- 11:22 UTC - Ingress controller read timeout lowered to terminate hung connections aggressively.
- 11:31 UTC - Active connection backlog clears; p99 latency stabilizes below 60ms.
- 11:39 UTC - Incident stood down; reporting query queue restored.

## Root cause
An unindexed reporting query scanned historical ledger entries, locking client HTTP connections on gateway-api for up to 60 seconds per request. Aggressive external client retries rapidly exhausted the gateway pod connection backlog.

## Resolution
Engineers applied an emergency ingress annotation to clamp upstream read timeouts to 15 seconds, dropping stalled requests before proxy sockets saturated:
```bash
kubectl annotate ingress gateway-ingress nginx.ingress.kubernetes.io/proxy-read-timeout="15" -n payments --overwrite
```

## Lessons
- Ingress gateway timeout policies must be decoupled between transactional and reporting endpoints.
- Upstream external clients must implement exponential jitter backoff on retryable HTTP errors.
- Enforce strict query execution timeouts at the database driver layer for all analytical requests.
