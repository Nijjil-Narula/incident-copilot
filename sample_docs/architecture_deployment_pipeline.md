# Architecture: CI/CD Deployment and Rollout Pipeline

## Deployment Overview
All services in the platform follow automated trunk-based continuous integration and deployment pipelines targeting the payments Kubernetes namespace.

## Build and Packaging
Code commits trigger automated testing, vulnerability scanning, and multi-stage container builds. Images are tagged with immutable commit SHAs before pushing to the container registry.

## Release Progression
Deployments proceed through structured stages:
1. **Automated Smoke Tests**: Container images deploy to ephemeral staging environments.
2. **Rolling Production Updates**: Services execute rolling updates with `maxSurge=25%` and `maxUnavailable=0` to preserve zero-downtime availability.
3. **Health Probing**: Kubernetes verifies readiness probes before directing live ingress traffic to newly spun pods.

## Health Probe Standards
Every microservice implements dedicated health endpoints:
- Liveness Probe: `GET /health/live` (monitors internal thread deadlocks)
- Readiness Probe: `GET /health/ready` (validates database and cache connectivity)

## Rollback Procedures
If post-deployment error rates increase after a release, engineers trigger an immediate deployment rollback:
```bash
kubectl rollout undo deployment/gateway-api -n payments
```
Verify rollout history and revision status:
```bash
kubectl rollout history deployment/gateway-api -n payments
```
