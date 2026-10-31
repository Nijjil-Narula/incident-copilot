# Runbook: Redis Cache Invalidation and Flush

## When to use
Use this runbook when corrupted or poisoned cache keys stored in Redis cause downstream 500 errors in auth-service, or when Redis memory exhaustion threatens service stability.

## Remediation Steps

1. Inspect memory allocation and eviction metrics on the Redis instance:
```bash
redis-cli -h redis info memory
```

2. Scan for contaminated or stale session cache keys:
```bash
redis-cli -h redis --scan --pattern "session:*"
```

3. If Redis memory remains critically exhausted or poisoned data persists:
**LAST RESORT**
```bash
redis-cli -h redis FLUSHALL ASYNC
```

4. Restart auth-service pods to reinitialize client connection pools:
```bash
kubectl rollout restart deployment/auth-service -n payments
```

## Verification
Confirm total database keys and operational metrics have normalized:
```bash
redis-cli -h redis dbsize
```
Verify that auth-service pods have restarted cleanly:
```bash
kubectl get pods -l app=auth-service -n payments
```

## Escalation
If PostgreSQL experiences connection saturation following cache warming, page the Core Services on-call engineer and enable rate limiting at gateway-api.
