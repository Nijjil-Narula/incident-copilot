# Post-mortem: Incident 404 - Redis MaxMemory Eviction Policy Failure

## Summary
On October 23, Redis rejected session write operations with OOM errors, preventing auth-service from persisting new user sessions.

## Impact
Users experienced login failures and session disconnects for 22 minutes. Read requests with existing tokens continued functioning until token expiration.

## Timeline
- 16:10 UTC - Spikes in 500 error rates reported by auth-service during user authentication.
- 16:15 UTC - Incident team assembles; logs reveal `OOM command not allowed when used memory > maxmemory`.
- 16:21 UTC - Redis configuration inspected; eviction policy found misconfigured to `noeviction`.
- 16:26 UTC - Runtime memory policy updated to Least Recently Used eviction.
- 16:32 UTC - Memory pressure recedes; session creation success rate recovers to 100%.

## Root cause
A recent infrastructure maintenance script restarted Redis with default configuration settings, overwriting the dynamic eviction policy from `allkeys-lru` to `noeviction`. When cache volume reached the memory ceiling, all write operations were rejected.

## Resolution
Engineers applied an emergency runtime configuration change to enable automatic LRU key eviction across all keyspaces:
```bash
redis-cli -h redis config set maxmemory-policy allkeys-lru
```

## Lessons
- Redis eviction policies must be codified in version-controlled ConfigMaps and checked by automated drift detection.
- Add memory threshold monitoring at 80% with alerting on memory policy drift.
- auth-service must gracefully degrade to PostgreSQL session verification when cache writes fail.
