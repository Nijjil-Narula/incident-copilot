# Runbook: TLS Certificate Expiry and Renewal

## When to use
Use this runbook when automated TLS certificate alerts notify that the gateway-api ingress certificate will expire in less than 7 days, or when external clients encounter SSL handshake errors.

## Remediation Steps

1. Inspect current certificate validity status and expiration window:
```bash
cmctl check certificate gateway-api-tls -n payments
```

2. Trigger immediate certificate renewal through cert-manager:
```bash
cmctl renew gateway-api-tls -n payments
```

3. Perform rolling restart of gateway-api pods to reload updated secret keys:
```bash
kubectl rollout restart deployment/gateway-api -n payments
```

## Verification
Inspect certificate secret details to confirm the new expiry timestamp:
```bash
cmctl inspect secret gateway-api-tls -n payments
```
Confirm gateway-api pods have reloaded successfully:
```bash
kubectl rollout status deployment/gateway-api -n payments
```

## Escalation
If cert-manager renewal fails due to DNS challenge errors, page the Security Operations on-call engineer and verify external DNS provider credentials.
