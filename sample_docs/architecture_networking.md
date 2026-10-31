# Architecture: Internal Mesh and Ingress Networking

## Ingress Architecture
External clients connect to the platform through gateway-api via ingress controllers configured in the payments namespace. TLS termination occurs at the gateway boundary using certificates managed by cert-manager.

## Internal Service Discovery
All services communicate using internal Kubernetes CoreDNS endpoints:
- gateway-api: `gateway-api.payments.svc.cluster.local:8000`
- auth-service: `auth-service.payments.svc.cluster.local:8080`
- ledger-service: `ledger-service.payments.svc.cluster.local:8081`
- reporting-service: `reporting-service.payments.svc.cluster.local:8082`
- postgres: `postgres.payments.svc.cluster.local:5432`
- redis: `redis.payments.svc.cluster.local:6379`
- kafka: `kafka.payments.svc.cluster.local:9092`

## Network Security Policies
Network policies restrict database and cache access:
- Only ledger-service and reporting-service may establish TCP connections to postgres on port 5432.
- Only auth-service and gateway-api may establish TCP connections to redis on port 6379.
- notification-worker is isolated from postgres and may only communicate with kafka on port 9092.

## Verification
Confirm network policies applied in the payments namespace:
```bash
kubectl get networkpolicies -n payments
```
Confirm active ingress routes:
```bash
kubectl get ingress -n payments
```
