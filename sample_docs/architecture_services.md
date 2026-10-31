# Architecture: Microservices Topology and Responsibilities

## Overview
The platform architecture consists of five core application services and three distributed stateful data stores deployed across the payments namespace.

## Service Responsibilities

### gateway-api
The edge reverse proxy and traffic router. Handles inbound HTTPS traffic, validates API contracts, enforces global rate limiting, and forwards validated requests to internal microservices.

### auth-service
Manages authentication token issuance, cryptographic verification, and session validation. Maintains fast in-memory user session state within redis.

### ledger-service
The transactional core for all financial processing. Implements strict ACID transactional updates and records all mutations directly into postgres before publishing events.

### reporting-service
An asynchronous analytics engine that executes long-running aggregation queries against dedicated read replicas. Never executes writes against the primary postgres instance.

### notification-worker
A distributed event processor that consumes messages from kafka topics and delivers outbound webhooks and push notifications.

## Data Store Topology

- **postgres**: Primary relational database cluster providing durable transaction storage for ledger-service and reporting-service.
- **redis**: Low-latency in-memory cache and session store utilized primarily by auth-service.
- **kafka**: Distributed event streaming platform buffering asynchronous domain events between ledger-service and notification-worker.

## Cluster Verification
Inspect deployment status across all core services:
```bash
kubectl get deployments -n payments
```
