# Runbook: High CPU Utilization on Core Services

## When to use
Use this runbook when CPU utilization alarms trigger for gateway-api exceeding 85% sustained over 5 minutes, resulting in degraded ingress response times or request queueing.

## Remediation Steps

1. Check CPU consumption across all gateway-api pods in the namespace:
```bash
kubectl top pods -l app=gateway-api -n payments
```

2. Scale out deployment replicas to distribute ingress load:
```bash
kubectl scale deployment/gateway-api --replicas=8 -n payments
```

3. If runaway worker threads fail to recover after scaling, perform a rolling restart:
```bash
kubectl rollout restart deployment/gateway-api -n payments
```

## Verification
Confirm that new pods are ready and serving traffic:
```bash
kubectl get pods -l app=gateway-api -n payments
```
Re-check pod metrics to ensure CPU consumption has dropped below 60%:
```bash
kubectl top pods -l app=gateway-api -n payments
```

## Escalation
If CPU utilization remains saturated across all scaled replicas, escalate to the Platform Performance on-call engineer and enable emergency request throttling at the external edge load balancer.
