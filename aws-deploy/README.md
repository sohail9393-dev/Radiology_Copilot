# Radiology Copilot — AWS deployment (portfolio demo)

Deploys the three-tier stack that already runs locally, swapping local MySQL for RDS.

This directory is **additive**: it does not change how the app runs locally. You can
delete `aws-deploy/` at any time.

## Architecture

```
                     ┌──────────────────────── Application Load Balancer ───────────────────────┐
  browser ──80/443──▶│  listener:80                                                                 │
                     │    path  /*        -> target group  rc-ui-tg   (Streamlit :8501)          │
                     │    path  /api/*    -> target group  rc-api-tg  (FastAPI   :8000, 1 worker)│
                     └───────────┬──────────────────────────────────────────────┬───────────────┘
                                 │                                              │
                     ┌───────────▼───────────┐                      ┌───────────▼───────────┐
                     │ ECS Fargate: rc-ui    │  API_BASE_URL =      │ ECS Fargate: rc-api   │
                     │ streamlit_app.py      │──http://rc-api:8000──▶ DenseNet121 + 4 Groq agents
                     │ 0.5 vCPU / 1 GB       │                      │ 1 vCPU / 2 GB         │
                     └───────────────────────┘                      └───────────┬───────────┘
                                                                                │ 3306
                                                                    ┌───────────▼───────────┐
                                                                    │ RDS MySQL 8.0 (private)│
                                                                    └───────────────────────┘

Secrets: AWS Secrets Manager (GROQ_API_KEY + MYSQL_*) -> injected as ECS task-definition secrets.
Config : SSM Parameter Store -> MYSQL_HOST (the RDS endpoint), API_BASE_URL.
Images : ECR (rc-api, rc-ui).
Logs   : CloudWatch Logs (/ecs/rc-api, /ecs/rc-ui).
```

## Why these choices

- **ECS Fargate + ALB, not App Runner.** App Runner is simpler, but reaching a private RDS from
  App Runner requires a VPC connector plus a NAT/endpoint setup for the image pull, and its
  per-service networking is fiddlier to reason about. Fargate gives us a normal VPC where the
  API task reaches RDS directly, and the images already ship correct `HEALTHCHECK`s that the
  ALB target groups reuse.
- **RDS, not MySQL-in-ECS.** Managed backups, patching, and a stable endpoint. The demo DB is
  tiny (`db.t4g.micro`).
- **One ALB, path-based routing.** `/api/*` reaches FastAPI, everything else reaches Streamlit.
  Set `API_BASE_URL` to the ALB's own `/api` path so the browser only ever sees one origin —
  no CORS configuration needed, because the browser talks only to Streamlit.

## Important: the API has no authentication

Anyone with the URL can upload images, read every study, and write clinician reviews.
That is acceptable for a **synthetic-data portfolio demo** and nothing else. Before putting
real patient data anywhere near this, see the compliance section in `README-DEPLOY.md`.

## Files

| File | Purpose |
|---|---|
| `README-DEPLOY.md` | Step-by-step runbook, cost estimate, teardown |
| `deploy.ps1` | Creates ECR, RDS, Secrets Manager, ALB, ECS services end-to-end |
| `teardown.ps1` | Deletes everything it created (stops all billing) |
| `taskdef-api.json` | ECS task definition template for the API (placeholders filled by `deploy.ps1`) |
| `taskdef-ui.json` | ECS task definition template for the UI |

## Prerequisites

- AWS CLI v2, authenticated as an admin (`aws sts get-caller-identity` works)
- Docker (for building/pushing images) — **not installed on this machine yet**
- A Groq API key
