# Architecture: Transaction and Event Data Flow

## Ingress Request Lifecycle
Client requests enter the cluster through gateway-api over TLS. The gateway inspects authentication headers and delegates token verification to auth-service via low-latency internal RPC.

## Authentication Verification
auth-service evaluates incoming request tokens against active session state cached in redis. If session data is not present in cache, auth-service reads user permissions from postgres and writes back to redis with a 15-minute TTL.

## Transaction Execution
Upon successful authentication, gateway-api routes transactional mutations to ledger-service:
1. ledger-service opens an explicit database transaction with postgres.
2. The state mutation is verified, committed, and locked in postgres.
3. Simultaneously with transaction commit, ledger-service produces a domain event to the corresponding kafka topic.

## Asynchronous Event Consumption
Downstream background workers consume events emitted to kafka:
- notification-worker consumes ledger event topics to trigger customer alerts.
- reporting-service consumes ledger summary topics to refresh materialised reporting views.

## Pipeline Inspection
Verify active topic subscription across consumer groups:
```bash
kafka-consumer-groups.sh --bootstrap-server kafka:9092 --list
```
Verify running pod state across transaction services:
```bash
kubectl get pods -l app=ledger-service -n payments
```
