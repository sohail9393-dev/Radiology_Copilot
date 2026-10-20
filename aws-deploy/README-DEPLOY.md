# Deployment runbook — Radiology Copilot on AWS (portfolio demo)

> **Honesty note.** Every artifact here is statically validated: the PowerShell scripts
> parse cleanly, both task-definition JSONs parse, and all 15 pinned dependency versions
> were confirmed against a working local venv (torch `2.14.0+cpu`, torchvision `0.29.0+cpu`,
> Python 3.11.9). **No `docker build` and no AWS API call has been executed** — Docker is
> not installed on this machine and it holds no AWS credentials. Expect to debug something
> on the first real run; the script is written to be re-runnable so you can iterate.

## Target architecture

```
browser ──▶ ALB :80 ──┬── /*      ──▶ ECS service rc-ui  (Streamlit :8501)
                      └── /api/*  ──▶ ECS service rc-api (FastAPI  :8000, 1 worker)
                                            │ server-side HTTP
                                            └──▶ http://rc-api.<region>.local:8000
                                            │
                                            └──▶ RDS MySQL 8.0 (private subnets, no public access)
```

Secrets live in **Secrets Manager** and are injected by ECS as container `secrets` —
nothing sensitive is baked into an image.

## Order of operations

**Infrastructure must exist before images.** ECS will fail to start tasks if the ECR repo
or image tag is missing, so run the steps in this order:

### 1. Deploy infrastructure (~10 min, mostly waiting on RDS)

```powershell
cd E:\projects\new_projects\Radiology_Copilot\aws-deploy
.\deploy.ps1 -Region us-east-1 -StackPassword 'pick-a-strong-db-password'
```

The Groq key is read from `..\.env` automatically (`GROQ_API_KEY`), or pass `-GroqApiKey`.
**Do not paste the key into a shell you do not control** — command history is a real leak vector.

### 2. Build and push images

Either locally (needs Docker Desktop):

```powershell
.\build-and-push.ps1 -Region us-east-1
```

Or in the cloud with no local Docker — push the repo to GitHub/CodeCommit first, then create a
CodeBuild project with [`buildspec.yml`](buildspec.yml), privileged mode enabled, and the
`AWS_ACCOUNT_ID` / `AWS_DEFAULT_REGION` env vars set.

```powershell
# after the first successful image push, roll the services
aws ecs update-service --region us-east-1 --cluster rc --service rc-api --force-new-deployment
aws ecs update-service --region us-east-1 --cluster rc --service rc-ui  --force-new-deployment
```

### 3. Verify

```powershell
$alb = aws elbv2 describe-load-balancers --region us-east-1 --names rc-alb --query "LoadBalancers[0].DNSName" --output text
Write-Host "Open http://$alb"

# API health directly, bypassing the ALB routing question:
aws logs tail /ecs/rc-api --follow --region us-east-1
```

Expect `GET /_stcore/health -> 200` on the UI target group and
`{"status":"ok","model_loaded":true}` in the API logs within ~2 minutes of task start
(model weights are baked into the image, so there is no download at boot).

## The `/api` path caveat — read this

An ALB **cannot strip a path prefix**. In this project FastAPI mounts routes at the root
(`/health`, `/docs`), so routing `/api/*` to the API target group means FastAPI receives
`/api/health` and returns **404**. Three ways to resolve it, pick one:

| Option | Change needed | Result |
|---|---|---|
| **A. Host-based routing** (cleanest) | Add a DNS name / Route 53 record; ALB rule on host header | `https://app.example.com` → UI, `https://api.example.com` → API, no app change |
| **B. `root_path`** | Set `root_path="/api"` in `FastAPI(...)` and add the `/api/*` rule | API reachable at `http://<alb>/api/docs` |
| **C. UI-only exposure** (simplest for a demo) | Do nothing | UI works; API is reached only server-side by the Streamlit container. `/docs` is not publicly reachable |

`deploy.ps1` deliberately does **not** add the `/api/*` rule, because doing so without
option B would break the API. The UI works out of the box either way.

## Guardrails already handled

- **`--platform linux/amd64` everywhere.** Fargate cannot run ARM images; building on an
  Apple Silicon or ARM machine without this flag yields a container that works locally and
  dies on Fargate with `exec format error`.
- **Service discovery, not service names.** On `awsvpc` Fargate there is no Docker DNS, so
  `http://api:8000` (which `docker-compose` provides) does not resolve. `deploy.ps1` creates a
  Cloud Map namespace and the UI uses `http://rc-api.<region>.local:8000`.
- **`assignPublicIp=ENABLED`** so tasks can pull from ECR and call the Groq API in the default
  VPC, which has no NAT gateway. Tasks still have no inbound exposure — the ALB is the only door.
- **Secrets by reference.** `GROQ_API_KEY` and the MySQL values are `valueFrom` Secrets Manager
  ARNs, so they never appear in the task definition, the image, or `docker inspect`.
- **One uvicorn worker.** Each worker loads its own copy of DenseNet121; more workers means
  duplicated memory for no throughput win on CPU.

## Cost

| Resource | Spec | ~USD/month |
|---|---|---|
| ALB | 1, ~$16–20 | 20 |
| ECS `rc-api` | 1 vCPU / 2 GB, always-on | 35 |
| ECS `rc-ui` | 0.5 vCPU / 1 GB, always-on | 18 |
| RDS | `db.t4g.micro`, 20 GB gp3, single-AZ | 15 |
| ECR + CloudWatch + Secrets | a few GB / secrets | 2 |
| **Total** | | **~90** |

To cut this materially: stop the tasks when idle
(`aws ecs update-service --desired-count 0`), and stop RDS when unused
(`aws rds stop-db-instance` — auto-restarts after 7 days). Running only when demoing can
bring it under $20/month.

## Teardown

```powershell
.\teardown.ps1 -Region us-east-1
```

Deletes everything `rc-`prefixed, including RDS **without a final snapshot** (deliberate — it is
a throwaway demo DB). Confirm nothing is left billing afterwards.

## Security posture — what this deployment is not

- **No authentication anywhere.** Anyone with the URL can upload images, read every study, and
  write clinician reviews. Acceptable only for synthetic demo data.
- **HTTP only, no TLS.** Add an ACM certificate and an HTTPS listener before using a real domain.
- **Groq sees the content** of every prompt, which includes CV findings and patient symptoms.
- **`RDS` is single-AZ with a 1-day backup window** — fine for a demo, not a durability story.

If this ever touches real patient data, stop and re-plan: you would need a BAA with AWS *and*
with Groq, encryption in transit and at rest, authentication in front of both services,
private networking, CloudTrail, and removal of the public `/docs`. That is a compliance
project, not a `docker push`.
