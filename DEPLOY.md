# Deploying EdgentRAG v3

A runbook, meant to be followed by hand — console-first, with CLI given as a
shortcut where it saves real time. Unlike v2's box, this is real
infrastructure with real edges: read a section fully before running its
commands, because later steps depend on values earlier ones print.

```
                        CloudFront (+ WAF)
                              │
                              ▼
                             ALB
                              │
                    EC2 Auto Scaling Group
                    web (nginx) + api, per instance
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
     RDS Postgres        ElastiCache          SQS (4 queues)
     (+ pgvector)          Redis              ingest · chat · stt · embed
                                                     │         │
                                          ┌──────────┘         └──────┐
                                          ▼                            ▼
                              ECS Fargate: ingest-worker      ECS Fargate: chat-worker
                                          │
                                          ▼
                                  S3 (unchanged bucket)
                                          ▲
                                          │  (presigned, both ways)
                                    Colab: embedding · stt · llm
                                    reaches the ALB/CloudFront's
                                    /broker endpoint — never AWS directly
```

**Contents**

1. [What you need before starting](#1-what-you-need-before-starting)
2. [RDS PostgreSQL](#2-rds-postgresql)
3. [ElastiCache Redis](#3-elasticache-redis)
4. [SQS queues](#4-sqs-queues)
5. [Secrets Manager and SSM Parameter Store](#5-secrets-manager-and-ssm-parameter-store)
6. [ECS: the two worker services](#6-ecs-the-two-worker-services)
7. [EC2: the Auto Scaling Group, and the ALB](#7-ec2-the-auto-scaling-group-and-the-alb)
8. [Cognito](#8-cognito)
9. [WAF](#9-waf)
10. [CloudFront](#10-cloudfront)
11. [Wiring it all together](#11-wiring-it-all-together)
12. [Verifying it actually works](#12-verifying-it-actually-works)
13. [Troubleshooting](#13-troubleshooting)
14. [Redeploying a code change](#14-redeploying-a-code-change)

---

## 1. What you need before starting

| | value | where it comes from |
|---|---|---|
| AWS account | `<aws-account-id>` | the project account — same as v1/v2 |
| Region | `us-east-1` | same as v1/v2 |
| S3 bucket | `<your-s3-bucket>` | v1/v2's bucket, reused as-is — do not recreate it |
| VPC | reuse the default VPC, or create one with 2 public + 2 private subnets across 2 AZs | needed for the ALB (public), and RDS/ElastiCache/ECS/EC2 (private) |
| Colab services | three `https://…trycloudflare.com` addresses | started separately, exactly as in v2 |
| Broker token | any random string | `openssl rand -hex 32`, same as v2 |

**No AWS access keys anywhere, at any step.** Every credential below is an
IAM role — an EC2 instance profile, an ECS task role, or nothing at all
(RDS/ElastiCache use passwords and network isolation, not IAM, for the
application's connection). If a step asks you to paste an access key
anywhere, stop: something above has gone wrong.

**Cost note.** RDS, ElastiCache, an ALB and NAT gateways (if your private
subnets need outbound internet — see §6) have a floor above zero before a
single user arrives. For a course project: the smallest Multi-AZ-off RDS
instance class, a single small ElastiCache node (no replica), and Fargate
tasks at the sizes in §6 keep that floor modest — a few tens of dollars a
month, not hundreds. Scale up only once you have a reason to.

---

## 2. RDS PostgreSQL

Console: **RDS → Create database.**

| field | value |
|---|---|
| Engine | PostgreSQL, a recent 15.x or newer (pgvector needs 15+) |
| Templates | Free tier, or Dev/Test if free tier's instance class is too small |
| DB instance identifier | `edgentrag-v3` |
| Master username | `edgentrag` |
| Master password | let RDS manage it in Secrets Manager (checkbox), or generate one and store it yourself in §5 |
| Instance class | `db.t4g.micro` or `db.t3.micro` for a course project |
| Storage | 20 GB gp3, autoscaling off (avoid a surprise bill from a runaway table) |
| Connectivity | **Don't** connect to an EC2 compute resource automatically — you will do the networking by hand |
| VPC | your VPC from §1 |
| Public access | **No** |
| VPC security group | create new: `edgentrag-v3-rds` |
| Availability Zone | any |
| Database authentication | Password authentication |
| Initial database name | `edgentrag` |

Create it. It takes several minutes. Note the **endpoint** (something like
`edgentrag-v3.xxxxxxxxxx.us-east-1.rds.amazonaws.com`) once it is `Available`.

**Security group**: edit `edgentrag-v3-rds`'s inbound rules to allow port
`5432` from two *other* security groups you will create in §6 and §7 (the ECS
workers' and the EC2 instances'), not from an IP or from `0.0.0.0/0`. You can
add these rules now with placeholder groups and fix them once those exist, or
come back to this after §6–7.

**pgvector**: nothing to do here yet — `CREATE EXTENSION vector` is one of
the Alembic migrations (`migrations/versions/0002_pgvector.py`), run in §6's
one-off migration task, not a console step. RDS grants that specific
extension to the master user by default; if it fails with a permissions
error, the engine version is too old (must be 15+).

**Build `DATABASE_URL`** (you will need this in §5):

```
postgresql://edgentrag:<password>@<the-endpoint>:5432/edgentrag
```

Not `postgres://` — that scheme fails at import (`shared/db.py::_driver`
rewrites it, but writing it correctly from the start avoids the one bug v2's
own TUTORIAL.md flagged as "a URL scheme that crashed everything").

---

## 3. ElastiCache Redis

Console: **ElastiCache → Redis caches → Create Redis cache.**

| field | value |
|---|---|
| Deployment option | Design your own cache |
| Creation method | Cluster cache, **cluster mode disabled** |
| Cluster mode | **Disabled — do not enable it.** `shared/events.py`'s pub/sub, which is what fans the SSE stream out across every EC2 instance, does not reliably cross shards in cluster mode. A single shard (one primary, optionally one replica) is what version 2's Redis usage already assumed, and this is that same assumption carried into a managed service. |
| Name | `edgentrag-v3` |
| Node type | `cache.t4g.micro` for a course project |
| Number of replicas | 0 (a replica is nice for availability, not required to pass this course) |
| Subnet group | create one across your private subnets |
| Security group | create new: `edgentrag-v3-redis` |
| Encryption in transit | **Enabled** — this is what makes the connection string `rediss://` instead of `redis://` |
| Encryption at rest | Enabled (default) |
| Auth | An AUTH token is optional at this scale; if you set one, it goes in Secrets Manager alongside the DB password in §5 |

Create it. Note the **primary endpoint** once available.

**Security group**: same pattern as RDS — allow port `6379` from the ECS
workers' and EC2 instances' security groups only.

**Build `REDIS_URL`**:

```
rediss://<primary-endpoint>:6379/0
```

`shared/events.py` and `shared/services.py` both use `redis.Redis.from_url` /
`redis.asyncio.Redis.from_url`, which understand `rediss://` natively — no
code change, config only.

---

## 4. SQS queues

Run this from anywhere with AWS credentials — your laptop, or CloudShell:

```bash
cd v3
pip install -r shared/requirements.txt -r backend/requirements.txt
python -m scripts.bootstrap
```

Creates **eight** queues — four for work, four dead-letter — prefixed
`edgentrag-v3-` (deliberately distinct from v2's `edgentrag-*`; see
`scripts/bootstrap.py`'s docstring for why). It prints:

```
INGEST_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<aws-account-id>/edgentrag-v3-ingest
CHAT_QUEUE_URL=https://…/edgentrag-v3-chat
STT_QUEUE_URL=https://…/edgentrag-v3-stt
EMBED_QUEUE_URL=https://…/edgentrag-v3-embed

BROKER_TOKEN=<a freshly generated one, if you had not set one already>
```

**Copy all five lines.** You need them in §5 and §11.

The script is idempotent — safe to re-run any time you lose the URLs.

---

## 5. Secrets Manager and SSM Parameter Store

**Secrets** (Secrets Manager — things that must not appear in a log or a task
definition's plain `environment` block):

| secret name | contents |
|---|---|
| `edgentrag-v3/database-url` | the full `DATABASE_URL` from §2 |
| `edgentrag-v3/redis-url` | the full `REDIS_URL` from §3 (only sensitive because it is an internal endpoint; treat it as a secret anyway — a stray Redis exposed to the world is a bad afternoon) |
| `edgentrag-v3/broker-token` | the `BROKER_TOKEN` from §4 |

**Parameters** (SSM Parameter Store, plain `String` type — things that are
fine in a task definition's `environment` block too, but centralising them
here means the EC2 launch template and the ECS task definitions read the same
values):

| parameter name | value |
|---|---|
| `/edgentrag-v3/s3-bucket` | `<your-s3-bucket>` |
| `/edgentrag-v3/aws-region` | `us-east-1` |
| `/edgentrag-v3/ingest-queue-url` | from §4 |
| `/edgentrag-v3/chat-queue-url` | from §4 |
| `/edgentrag-v3/stt-queue-url` | from §4 |
| `/edgentrag-v3/embed-queue-url` | from §4 |
| `/edgentrag-v3/cognito-region` | from §8, once you get there |
| `/edgentrag-v3/cognito-user-pool-id` | from §8 |
| `/edgentrag-v3/cognito-app-client-id` | from §8 |

You can come back and fill in the three Cognito parameters after §8 — nothing
before then reads them.

---

## 6. ECS: the two worker services

### 6.1 Push the images

Build both from the **v3 repo root** — this is what lets each image copy
`shared/` in without a published package:

```bash
aws ecr create-repository --repository-name edgentrag-v3/ingest-worker
aws ecr create-repository --repository-name edgentrag-v3/chat-worker

aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com

docker build -f workers/ingest/Dockerfile -t <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/ingest-worker:latest .
docker build -f workers/chat/Dockerfile   -t <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/chat-worker:latest .

docker push <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/ingest-worker:latest
docker push <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/chat-worker:latest
```

Also push the API image now — the one-off migration task in §6.4 uses it:

```bash
aws ecr create-repository --repository-name edgentrag-v3/api
docker build -f backend/Dockerfile -t <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/api:latest .
docker push <aws-account-id>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/api:latest
```

### 6.2 The cluster

Console: **ECS → Clusters → Create cluster.** Name it `edgentrag-v3`,
infrastructure **AWS Fargate**. Nothing else to configure — no EC2 capacity
providers.

### 6.3 IAM: two task roles, scoped narrowly

Console: **IAM → Roles → Create role → AWS service → Elastic Container
Service → Elastic Container Service Task.**

**`edgentrag-v3-ingest-task-role`** — inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ConsumeIngest",
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"],
      "Resource": "arn:aws:sqs:us-east-1:<aws-account-id>:edgentrag-v3-ingest"
    },
    {
      "Sid": "QueueFollowUpJobs",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": [
        "arn:aws:sqs:us-east-1:<aws-account-id>:edgentrag-v3-stt",
        "arn:aws:sqs:us-east-1:<aws-account-id>:edgentrag-v3-embed"
      ]
    },
    {
      "Sid": "Bucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<your-s3-bucket>/*"
    }
  ]
}
```

`ConsumeIngest` is this worker's main loop (`shared/worker.py` polling the
`ingest` queue). `QueueFollowUpJobs` is for `_request_transcription` (queues
an `stt` job) and `_store_and_queue` (queues an `embed` job) in
`workers/ingest/main.py` — this worker only ever *sends* to `stt`/`embed`,
never receives from them; the GPU consumes those two through the broker.
Note this role needs no `ReceiveMessage` on `embed` at all: the `"vectorize"`
follow-up message that eventually comes back is queued onto `ingest` (which
this role already consumes) by the API process in `api/routes/broker.py`,
not sent here.

**`edgentrag-v3-chat-task-role`** — inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Queue",
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"],
      "Resource": "arn:aws:sqs:us-east-1:<aws-account-id>:edgentrag-v3-chat"
    }
  ]
}
```

No S3 permission — v3's chat worker never touches storage; retrieval is a
database query now.

Both roles also need a **task execution role** (the ECS-managed one,
`AmazonECSTaskExecutionRolePolicy`) plus permission to read the three secrets
from §5:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": [
      "arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:edgentrag-v3/database-url-*",
      "arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:edgentrag-v3/redis-url-*",
      "arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:edgentrag-v3/broker-token-*"
    ]
  }]
}
```

### 6.4 Migrate the schema, once, before anything else starts

```bash
aws ecs run-task \
  --cluster edgentrag-v3 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-ids>],securityGroups=[<ecs-sg>],assignPublicIp=DISABLED}" \
  --task-definition edgentrag-v3-migrate \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","-m","scripts.migrate"]}]}'
```

Register `edgentrag-v3-migrate` as a task definition first: the API image
from §6.1, the execution role from §6.3, `DATABASE_URL` from the secret in
§5. Watch its logs in CloudWatch — it should print `applying migrations up to
head` then `done`. **Do this before starting the worker services or the EC2
Auto Scaling Group** — both assume the schema (and the pgvector extension)
already exist.

### 6.5 Task definitions

| | ingest-worker | chat-worker |
|---|---|---|
| CPU / memory | 1 vCPU / 2 GB (raise to 2/4 if OCR on scanned PDFs proves slow) | 0.5 vCPU / 1 GB |
| Image | `.../edgentrag-v3/ingest-worker:latest` | `.../edgentrag-v3/chat-worker:latest` |
| Task role | `edgentrag-v3-ingest-task-role` | `edgentrag-v3-chat-task-role` |
| Secrets (from §5) | `DATABASE_URL`, `REDIS_URL`, `BROKER_TOKEN` | `DATABASE_URL`, `REDIS_URL` |
| Environment | `S3_BUCKET`, `AWS_REGION`, `INGEST_QUEUE_URL`, `EMBED_QUEUE_URL`, `STT_QUEUE_URL` | `AWS_REGION`, `CHAT_QUEUE_URL` |
| `stopTimeout` | 120 (ECS's max) | 60 |
| Logging | `awslogs`, group `/ecs/edgentrag-v3-ingest` | group `/ecs/edgentrag-v3-chat` |

### 6.6 Services

**ECS → Clusters → edgentrag-v3 → Create service** for each, launch type
Fargate, private subnets, the worker's own security group (`edgentrag-v3-ecs`,
outbound-only — these have no inbound listener at all), desired count **1**
to start.

### 6.7 Autoscaling

**Application Auto Scaling**, target tracking, on a **custom CloudWatch
metric** — `ApproximateNumberOfMessagesVisible` for the queue divided by the
service's running task count (a metric-math expression, not a raw SQS
metric):

| service | target value | min | max |
|---|---|---|---|
| ingest-worker | 1–2 messages/task | 1 | 3 |
| chat-worker | 1–2 messages/task | 1 | 2 |

This is the same ratio pattern `../v2/V3_DESIGN.html` sketches — proportional
to backlog-per-worker, not "scale on any nonzero depth." `min=1` so a single
upload never waits on a cold Fargate start.

---

## 7. EC2: the Auto Scaling Group, and the ALB

### 7.1 IAM instance role

**IAM → Roles → Create role → AWS service → EC2.** Name it
`edgentrag-v3-ec2`. Inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Queues",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:us-east-1:<aws-account-id>:edgentrag-v3-*"
    },
    {
      "Sid": "Bucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<your-s3-bucket>/*"
    },
    {
      "Sid": "Secrets",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:edgentrag-v3/database-url-*",
        "arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:edgentrag-v3/redis-url-*",
        "arn:aws:secretsmanager:us-east-1:<aws-account-id>:secret:edgentrag-v3/broker-token-*"
      ]
    },
    {
      "Sid": "Parameters",
      "Effect": "Allow",
      "Action": "ssm:GetParameters",
      "Resource": "arn:aws:ssm:us-east-1:<aws-account-id>:parameter/edgentrag-v3/*"
    }
  ]
}
```

Note this instance no longer needs `sqs:ReceiveMessage` on anything — the API
only ever *sends* to queues, it never consumes. That is a smaller grant than
v2's instance role, and worth noticing: separating the workers out actually
narrowed what the API's own credential can do.

### 7.2 Security groups

- `edgentrag-v3-alb` — inbound 443 from `0.0.0.0/0` (or, once §10 is done,
  from CloudFront's managed prefix list only — tighter, and the recommended
  end state).
- `edgentrag-v3-ec2` — inbound 8080 from `edgentrag-v3-alb` only.
- Go back and add inbound rules on `edgentrag-v3-rds` (5432) and
  `edgentrag-v3-redis` (6379) allowing `edgentrag-v3-ec2` and the ECS
  workers' security group, if you have not already.

### 7.3 Launch template

**EC2 → Launch Templates → Create launch template.**

| field | value |
|---|---|
| Name | `edgentrag-v3` |
| AMI | Ubuntu 24.04 LTS (same base as v1/v2, for continuity) |
| Instance type | `t3.medium` to start |
| Key pair | yours, for troubleshooting SSH access |
| Security group | `edgentrag-v3-ec2` |
| IAM instance profile | `edgentrag-v3-ec2` |
| User data | see below |

User data (fetches config, builds `.env`, starts the stack):

```bash
#!/bin/bash
set -euo pipefail
curl -fsSL https://get.docker.com | sh
usermod -aG docker ubuntu

REGION=us-east-1
mkdir -p /opt/edgentrag && cd /opt/edgentrag

# Pull the code -- either git clone a repo you have pushed this to, or rsync
# it up ahead of time and bake it into a custom AMI. A plain git clone is
# simplest for a course project:
git clone <your-repo-url> app && cd app/v3

param() { aws ssm get-parameter --region "$REGION" --name "$1" --query Parameter.Value --output text; }
secret() { aws secretsmanager get-secret-value --region "$REGION" --secret-id "$1" --query SecretString --output text; }

cat > .env <<EOF
ENV=aws
DATABASE_URL=$(secret edgentrag-v3/database-url)
REDIS_URL=$(secret edgentrag-v3/redis-url)
S3_BUCKET=$(param /edgentrag-v3/s3-bucket)
AWS_REGION=$REGION
INGEST_QUEUE_URL=$(param /edgentrag-v3/ingest-queue-url)
CHAT_QUEUE_URL=$(param /edgentrag-v3/chat-queue-url)
STT_QUEUE_URL=$(param /edgentrag-v3/stt-queue-url)
EMBED_QUEUE_URL=$(param /edgentrag-v3/embed-queue-url)
BROKER_TOKEN=$(secret edgentrag-v3/broker-token)
COGNITO_REGION=$(param /edgentrag-v3/cognito-region)
COGNITO_USER_POOL_ID=$(param /edgentrag-v3/cognito-user-pool-id)
COGNITO_APP_CLIENT_ID=$(param /edgentrag-v3/cognito-app-client-id)
WEB_PORT=8080
EOF

docker compose -f docker-compose.ec2.yml up -d --build
```

**Why docker-compose-per-instance, not bare `docker run`**: it continues
version 2's exact mental model — `web` and `api`, two containers, the same
internal DNS name (`api:8000`) the unmodified `nginx.conf` already proxies to
— without reimplementing restart policy and inter-container networking by
hand for no benefit at this scale.

### 7.4 Target group and ALB

**EC2 → Target groups → Create target group.** Instance target type, HTTP,
port 8080, health check path **`/api/health`** (not `/api/ready` — see
`backend/api/routes/health.py`'s docstring for why: a brief RDS blip must
not deregister every healthy instance at once).

**EC2 → Load Balancers → Create → Application Load Balancer.** Internet-facing,
public subnets, security group `edgentrag-v3-alb`, listener 443 (needs an ACM
certificate — see below) forwarding to the target group above.

**Set the ALB's idle timeout to 300 seconds** (Load Balancer → Attributes →
Idle timeout). The default, 60s, is a separate setting from nginx's own
`proxy_read_timeout 24h` on the SSE path — comfortably above the app's 15s
SSE heartbeat is the point, and 300s leaves real margin. Easy to miss by
hand; there is nothing in the app that surfaces a missed setting here except
a stream that drops every few minutes.

**ACM certificate**: request one in `us-east-1` for your domain (Certificate
Manager → Request → Public certificate), validate it via DNS, attach it to
the ALB's 443 listener. If you do not have a domain yet, request one in
Route 53 first — v2's "the tunnel URL changes on every restart" problem is
exactly what owning a domain here is for.

### 7.5 Auto Scaling Group

**EC2 → Auto Scaling Groups → Create.** Launch template from §7.3, private
subnets (the ASG's instances should not need a public IP — outbound internet,
for pulling the Docker base images and talking to Colab, can go through a NAT
gateway in the same VPC, or you can put the ASG in public subnets with a
public IP if you want to skip NAT's cost for a course project). Attach to the
target group from §7.4.

| field | value |
|---|---|
| Desired capacity | 2 |
| Minimum | 2 |
| Maximum | 4 |

Two, not one, from the start — this is the whole reason the ALB exists: a
deploy or an instance failure should never take the app fully down. Scale
target-tracking on average CPU or request count per target if you want it
automatic; fixed at 2–4 is a fine, honest answer for a course project's
traffic.

---

## 8. Cognito

**Cognito → User pools → Create user pool.**

| field | value |
|---|---|
| Sign-in options | Email |
| Password policy | Cognito defaults are fine |
| MFA | Optional, off for a course project |
| Self-service sign-up | Enabled |
| Required attributes | email |
| Pool name | `edgentrag-v3` |

**App client**: Create one, name it `edgentrag-v3-web`. **Uncheck "Generate a
client secret"** — a secret-bearing client cannot be used safely from browser
JavaScript, and `shared/auth.py` never expects one. No Hosted UI needed; the
frontend talks to Cognito directly via `amazon-cognito-identity-js`
(`frontend/src/auth.js`).

**Group**: **Cognito → your pool → Groups → Create group.** Name it exactly
`admins`. Add your own user to it once you have signed up through the app —
this is what `shared/auth.py::require_admin` checks for `/config/services`.

Note three values: the pool's **Region**, its **User pool ID**
(`us-east-1_xxxxxxxxx`), and the app client's **Client ID**. Put them in:

- SSM parameters `/edgentrag-v3/cognito-region`,
  `/edgentrag-v3/cognito-user-pool-id`, `/edgentrag-v3/cognito-app-client-id`
  (§5) — read by the EC2 launch template's user data.
- `frontend/.env.production`'s `VITE_COGNITO_*` variables, **before**
  building the frontend image — these are baked in at build time, unlike the
  backend's, which are read at container start.

Rebuild and redeploy the `web` image after filling these in (§14).

---

## 9. WAF

**WAF & Shield → Web ACLs → Create web ACL.** Resource type
**CloudFront distributions** (not the ALB — see the rationale below), region
**Global (CloudFront)**.

Add these managed rule groups:

- `AWSManagedRulesCommonRuleSet`
- `AWSManagedRulesKnownBadInputsRuleSet`
- `AWSManagedRulesAmazonIpReputationList`

Add one rate-based rule: **2000 requests per 5-minute window, per IP**,
action Block. Generous enough that a normal person uploading files and
chatting never trips it; tight enough to stop a runaway script.

**Why CloudFront and not the ALB**: CloudFront is the actual public entry
point for both static assets and `/api/*` once §10 is done. Filtering there
blocks bad traffic before it ever reaches the ALB, EC2, or RDS; a
WAF attached to the ALB only ever sees what CloudFront already decided to
forward.

You will attach this Web ACL to the CloudFront distribution *while creating
it* in §10 — create the Web ACL first so it is available in that dropdown.

**Note for the troubleshooting section**: a client stuck in a broken SSE
reconnect loop (an expired or bad ticket, retried aggressively) is the one
realistic way a normal user's own browser could approach the rate limit. See
§13.

---

## 10. CloudFront

**CloudFront → Create distribution.**

| field | value |
|---|---|
| Origin domain | the ALB's DNS name |
| Origin protocol | HTTPS (match the ALB's 443 listener) |
| Web ACL | the one from §9 |
| Viewer protocol policy | Redirect HTTP to HTTPS |

**Behaviors**, in this order (CloudFront evaluates path patterns most-specific-first automatically, but the SSE pattern must still be entered so it exists as its own behavior, not folded into `/api/*`):

| path pattern | cache policy | origin request policy |
|---|---|---|
| `/api/sessions/*/events` | **CachingDisabled** | **AllViewer** |
| `/api/*` | **CachingDisabled** | **AllViewer** |
| `Default (*)` | **CachingOptimized** | (none needed) |

**`AllViewer` on both API behaviors is not optional.** CloudFront's default
policies strip most headers, cookies and query strings before forwarding.
Without `AllViewer`, the `Authorization: Bearer …` header every authenticated
call now carries is silently dropped, and every call fails at the edge with
a 401/403 that has nothing to do with the backend — the single easiest thing
to misconfigure here. The SSE path's `?ticket=` query parameter has the same
requirement.

**Origin response timeout**: Distribution → Origins → edit the ALB origin →
**Origin response timeout: 60 seconds** (default 30). Margin above the app's
15-second SSE heartbeat.

**Custom domain**: add your Route 53 domain as an alternate domain name, with
a certificate from ACM **in `us-east-1`** specifically (CloudFront requires
its certificate there regardless of which region everything else is in — a
different requirement from the ALB's, which uses a regional certificate).
Point the domain's DNS at the CloudFront distribution.

Once this is live, go back to §7.2 and tighten `edgentrag-v3-alb`'s inbound
rule to CloudFront's managed prefix list only, so the ALB cannot be reached
by skipping CloudFront (and therefore WAF) entirely.

---

## 11. Wiring it all together

One table, so nothing is set twice inconsistently between the EC2 user data
and the ECS task definitions:

| `Settings` field | EC2 (docker-compose.ec2.yml) | ECS (ingest/chat task defs) |
|---|---|---|
| `DATABASE_URL` | Secrets Manager, via user data | Secrets Manager, task def `secrets` |
| `REDIS_URL` | Secrets Manager | Secrets Manager |
| `S3_BUCKET` | SSM parameter | plain `environment` |
| `AWS_REGION` | SSM parameter | plain `environment` |
| `INGEST_QUEUE_URL` / `CHAT_QUEUE_URL` / `STT_QUEUE_URL` / `EMBED_QUEUE_URL` | SSM parameters | plain `environment` (each task only needs the ones it uses — §6.5) |
| `BROKER_TOKEN` | Secrets Manager | Secrets Manager (ingest only — chat never talks to the broker) |
| `COGNITO_REGION` / `COGNITO_USER_POOL_ID` / `COGNITO_APP_CLIENT_ID` | SSM parameters | not needed — workers never verify a user's token, only the API does |
| `VITE_COGNITO_*` | baked into the `web` image at build time | n/a |

Point the GPU at the new public endpoint, same two directions as v2:

**Us → them** (interactive calls): open the CloudFront URL, paste the three
Colab addresses into the app's first screen, exactly as before.

**Them → us** (queued work): on Colab, in `services/.env`:

```ini
BROKER_URL=https://your-domain.example/api
BROKER_TOKEN=<the same value from Secrets Manager>
```

---

## 12. Verifying it actually works

1. Open the CloudFront URL. Sign up, confirm the email code, sign in.
2. Enter the three Colab addresses.
3. Upload a small `.txt` file and a short video.
4. Watch progress arrive over the event stream (not polling — open the
   browser's network tab and confirm you see one long-lived `/events`
   connection, not repeated `/status` calls).
5. Ask a question. The answer should appear.

Then confirm the parts that are new in v3 specifically:

```bash
# Vectors landed in Postgres, not just Chroma:
psql "$DATABASE_URL" -c "select count(*) from chunks where embedding is not null;"
# should be > 0 once step 3 above finishes indexing

# The broker still works from Colab's side:
curl -s -X POST https://your-domain.example/api/broker/claim \
  -H "Authorization: Bearer $BROKER_TOKEN" -H "Content-Type: application/json" \
  -d '{"job":"embed"}'
# 200 with a job, or 204 if the queue is empty -- either is "working"

# Queue depth, same endpoint as v2:
curl -s https://your-domain.example/api/metrics/queues | python3 -m json.tool
```

---

## 13. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| every API call returns 401/403 through CloudFront but works when curled directly at the ALB | the `/api/*` CloudFront behavior is not using the `AllViewer` origin request policy, so `Authorization` is being stripped | §10 |
| the SSE connection opens then immediately closes, repeatedly | either the ALB idle timeout is still at its 60s default, or the CloudFront behavior for the events path is missing/uses a caching policy | §7.4, §10 |
| `psql`/the app can't reach RDS or Redis at all | a security group is missing the caller's SG in its inbound rule | §2, §3, §7.2 |
| a file stays "processing" forever, no error | check the ingest-worker's CloudWatch logs first, then the `edgentrag-v3-embed-dlq` queue depth — five failed attempts land there | ECS service logs; `aws sqs get-queue-attributes --queue-url <dlq-url> --attribute-names ApproximateNumberOfMessages` |
| `AccessDenied` on a worker's first SQS or S3 call | a task role is missing an action, or points at the wrong queue ARN | re-check §6.3 against the exact queue URLs from §4 |
| pgvector writes silently do nothing, or raise a type error | `register_vector` never ran — confirm `DATABASE_URL` really is `postgresql://` (not still SQLite) in the worker's environment | `shared/db.py`'s `_register_pgvector` hook only registers when `_is_postgres()` |
| `CREATE EXTENSION vector` fails during migration | RDS engine version is older than 15, or the migration ran against the wrong instance | confirm the engine version in §2 |
| a legitimate user gets rate-limited by WAF | almost always a stuck SSE reconnect loop against an expired ticket, retried aggressively by the browser | confirm in CloudWatch (WAF sample requests); the fix is client-side — the ticket TTL (`sse_ticket_seconds`) may need raising, or the frontend needs a backoff on repeated ticket failures |
| the migration task exits immediately, `DATABASE_URL is sqlite; nothing to migrate` | the ECS task definition's `DATABASE_URL` secret is not wired, so `shared/config.py`'s default (a sqlite path) is what actually loaded | check the task definition's `secrets` block, not just `environment` |

---

## 14. Redeploying a code change

**The API / frontend (EC2):**

```bash
ssh into an instance, or update the launch template + trigger an
instance refresh on the Auto Scaling Group:

aws autoscaling start-instance-refresh --auto-scaling-group-name edgentrag-v3
```

If the code lives in a git repo the user-data script clones, a simple
`git pull && docker compose -f docker-compose.ec2.yml up -d --build` on each
instance (or an instance refresh, which replaces them with fresh ones running
the current user-data) covers it. Rolling — the ALB drains connections from
an instance before it is replaced, so no request is dropped mid-flight.

**A worker (ECS):**

```bash
docker build -f workers/ingest/Dockerfile -t .../edgentrag-v3/ingest-worker:latest .
docker push .../edgentrag-v3/ingest-worker:latest
aws ecs update-service --cluster edgentrag-v3 --service ingest-worker --force-new-deployment
```

ECS starts new tasks, waits for them to be healthy, then stops the old ones —
the same graceful-SIGTERM handling in `shared/worker.py` that lets a rolling
worker deploy never duplicate in-flight work.

**A schema change**: add a new Alembic migration, then re-run §6.4's
`run-task` command before deploying any code that depends on the new schema.
