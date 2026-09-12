# Deployment plan — EdgentRAG v3, service by service

`DEPLOY.md` is the **runbook**: every console field, every command, in one
long pass. This file is the **plan**: what the independently deployable units
are, what each one needs before it can start, in what order to bring them up,
and how to prove each one works before moving to the next.

Read this first to know *where you are*. Read `DEPLOY.md`'s numbered sections
for the actual clicks. Every unit below cites the `DEPLOY.md` section that
does the work — shown as §N.

---

## The deployable units

There are **six** things you deploy, plus one foundation layer that is not a
service. "Independently deployable" means: it has its own build artifact, its
own release step, and you can push a change to it without touching the others.

| # | unit | artifact | runs on | release step |
|---|---|---|---|---|
| 0 | **Foundation** | none — provisioned resources | AWS | console / `scripts/bootstrap.py` |
| 1 | **Schema** | the API image | one-off ECS Fargate task | `aws ecs run-task … scripts.migrate` |
| 2 | **ingest-worker** | `edgentrag-v3/ingest-worker` | ECS Fargate service | `aws ecs update-service --force-new-deployment` |
| 3 | **chat-worker** | `edgentrag-v3/chat-worker` | ECS Fargate service | `aws ecs update-service --force-new-deployment` |
| 4 | **api + web** | `edgentrag-v3/api` + the `web` image | EC2 Auto Scaling Group behind an ALB | instance refresh, or `git pull && docker compose up -d --build` |
| 5 | **Edge** | none — WAF + CloudFront config | AWS global | console |
| 6 | **AI services** | the `services/` folder on Drive | Colab GPU | `bash run_colab.sh` |

Units 2, 3 and 4 are the three programs that share `shared/`. That directory
is top-level precisely so each image can `COPY shared /app/shared` and ship on
its own schedule.

---

## Dependency graph

```
                        ┌──────────────────────────────┐
                        │ 0. Foundation                │
                        │  VPC/SGs · RDS · ElastiCache │
                        │  SQS ×8 · Secrets · SSM      │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────▼───────────────┐
                        │ 1. Schema (one-off task)     │  ← BLOCKS everything below
                        │  alembic upgrade head        │
                        │  + CREATE EXTENSION vector   │
                        └──────────────┬───────────────┘
                   ┌───────────────────┼───────────────────┐
                   ▼                   ▼                   ▼
        ┌────────────────┐  ┌────────────────┐  ┌──────────────────────┐
        │ 2. ingest      │  │ 3. chat        │  │ 4. api + web (EC2)   │
        │    worker      │  │    worker      │  │    needs Cognito ids │
        └────────────────┘  └────────────────┘  │    baked in at build │
                                                └──────────┬───────────┘
                                                           ▼
                                                ┌──────────────────────┐
                                                │ 5. Edge: WAF →       │
                                                │    CloudFront        │
                                                └──────────┬───────────┘
                                                           ▼
                                                ┌──────────────────────┐
                                                │ 6. Colab AI services │
                                                │  needs the public    │
                                                │  URL for BROKER_URL  │
                                                └──────────────────────┘
```

Units 2, 3 and 4 are **parallel** — they share no build artifact and no
ordering constraint between them. Everything else is a hard chain.

---

## Recommended order — and two deviations from DEPLOY.md

Follow `DEPLOY.md`'s sections, but reorder two things. Both are real
time-savers, not preferences.

### Deviation 1 — do Cognito (§8) early, right after §5

`DEPLOY.md` puts Cognito at §8, after the EC2 Auto Scaling Group at §7. That
ordering forces a rebuild, and `DEPLOY.md` says so itself: *"Rebuild and
redeploy the `web` image after filling these in (§14)."*

The reason is a genuine asymmetry in how the two halves read config:

| | where `COGNITO_*` comes from | when |
|---|---|---|
| backend (api) | `.env`, written by EC2 user data from SSM | **container start** — restart picks it up |
| frontend (web) | `VITE_COGNITO_*` in `frontend/.env.production` | **`vite build`** — baked into the JS bundle |

So the frontend bundle is only correct if the pool exists *before* the image
is built. Create the User Pool, the app client and the `admins` group right
after §5, write the three values into SSM **and** into
`frontend/.env.production`, and §7's first launch is also its last.

### Deviation 2 — create the empty security groups before §2

`DEPLOY.md` §2 acknowledges the circularity: the RDS security group needs to
allow the ECS and EC2 groups, which do not exist yet, so *"add these rules now
with placeholder groups and fix them once those exist."*

Cleaner: create all four groups empty, first, then reference them freely as
you go.

```
edgentrag-v3-alb     (inbound 443)
edgentrag-v3-ec2     (inbound 8080 from -alb)
edgentrag-v3-ecs     (no inbound at all — outbound only)
edgentrag-v3-rds     (inbound 5432 from -ec2, -ecs)
edgentrag-v3-redis   (inbound 6379 from -ec2, -ecs)
```

Security groups cost nothing and reference each other by ID, so an empty one
is a perfectly good forward declaration.

### The resulting order

```
Phase A  foundation      §1 → SGs → §2 RDS → §3 Redis → §4 SQS → §5 secrets
Phase B  identity        §8 Cognito          ← moved up
Phase C  schema          §6.1 images → §6.3 IAM → §6.4 migrate   ← GATE
Phase D  compute         §6.5–6.7 workers  ‖  §7 EC2 + ALB       ← parallel
Phase E  edge            §9 WAF → §10 CloudFront
Phase F  connect         §11 wiring → §6 Colab → §12 verify
```

---

## Phase A — Foundation

**Deploys:** nothing yet. Produces the values everything else consumes.
**DEPLOY.md:** §1, §2, §3, §4, §5

| step | produces | notes |
|---|---|---|
| Security groups (all five, empty) | SG ids | see Deviation 2 |
| RDS PostgreSQL 15+ | `DATABASE_URL` | `postgresql://` — **never** `postgres://` |
| ElastiCache Redis | `REDIS_URL` | **cluster mode disabled**; TLS on → `rediss://` |
| `python -m scripts.bootstrap` | 4 queue URLs + `BROKER_TOKEN` | creates 8 queues (4 work + 4 DLQ), idempotent |
| Secrets Manager ×3 | ARNs | database-url, redis-url, broker-token |
| SSM Parameter Store ×9 | paths | bucket, region, 4 queue URLs, 3 Cognito (fill in Phase B) |

**Two settings here have no error message if you get them wrong:**

- **Cluster mode must be disabled.** Redis pub/sub does not reliably cross
  shards, and pub/sub is what fans the SSE stream out across every EC2
  instance (`shared/events.py`). Enable it and progress events silently stop
  reaching some browsers. There is no log line for this.
- **`postgresql://`, not `postgres://`.** RDS consoles emit the latter.
  `shared/db.py::_driver` rewrites it, but v2's own tutorial flagged this
  scheme as "a URL scheme that crashed everything" — write it correctly.

**Gate — do not continue until all three pass:**

```bash
# RDS reachable and version 15+
psql "$DATABASE_URL" -c "select version();"

# Redis reachable over TLS
redis-cli -u "$REDIS_URL" --tls ping          # → PONG

# All four queues exist
aws sqs list-queues --queue-name-prefix edgentrag-v3 --output text | wc -l   # → 8
```

---

## Phase B — Cognito

**Deploys:** identity. **DEPLOY.md:** §8 (moved up — see Deviation 1)

1. User pool `edgentrag-v3`, sign-in by **email**, self-service sign-up on.
2. App client `edgentrag-v3-web` — **uncheck "Generate a client secret."** A
   secret-bearing client cannot be used from browser JavaScript, and
   `shared/auth.py` never expects one.
3. Group named exactly **`admins`**. This is what `require_admin` checks for
   `PUT /config/services` — the call that repoints the Colab addresses. Add
   yourself to it after you first sign up through the app.
4. Write Region / User Pool ID / App Client ID to **both**:
   - the three SSM parameters from §5 (read by EC2 user data at boot)
   - `frontend/.env.production`'s `VITE_COGNITO_*` (baked in at `vite build`)

**Gate:**

```bash
# JWKS is public and must be fetchable — shared/auth.py fetches this on every
# cold cache miss. A 404 here means the pool id is wrong.
curl -s "https://cognito-idp.$REGION.amazonaws.com/$USER_POOL_ID/.well-known/jwks.json" \
  | python3 -m json.tool | head
```

> **Escape hatch:** leave all three `COGNITO_*` blank and the API runs with no
> auth at all (`shared/auth.py::cognito_configured` → every check skipped,
> `require_user` returns `"local-dev"`). Useful to get Phases C–D working
> before identity is in play. Do not ship it that way.

---

## Phase C — Images, IAM, and the schema

**Deploys:** unit 1. **DEPLOY.md:** §6.1, §6.2, §6.3, §6.4

### C1. Build and push three images (§6.1)

All from the **v3 repo root** — that is what lets each Dockerfile copy
`shared/` in without a published package.

```bash
ACCOUNT=<your-account-id>; REGION=us-east-1
REG=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com

for r in api ingest-worker chat-worker; do
  aws ecr create-repository --repository-name edgentrag-v3/$r 2>/dev/null || true
done

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REG

docker build -f backend/Dockerfile        -t $REG/edgentrag-v3/api:latest           .
docker build -f workers/ingest/Dockerfile -t $REG/edgentrag-v3/ingest-worker:latest .
docker build -f workers/chat/Dockerfile   -t $REG/edgentrag-v3/chat-worker:latest   .

docker push $REG/edgentrag-v3/api:latest
docker push $REG/edgentrag-v3/ingest-worker:latest
docker push $REG/edgentrag-v3/chat-worker:latest
```

The API image is needed now because the migration task runs from it — it
already bundles `migrations/` and `alembic` (see `scripts/migrate.py`'s
docstring).

### C2. Task roles (§6.3)

Two task roles, scoped to exactly what each worker does:

| role | SQS | S3 |
|---|---|---|
| `edgentrag-v3-ingest-task-role` | receive/delete/change-visibility on **ingest**; send on **stt**, **embed** | get/put on the bucket |
| `edgentrag-v3-chat-task-role` | receive/delete/change-visibility on **chat** | **none** |

Two things worth understanding rather than copying:

- The ingest role needs **no** `ReceiveMessage` on `embed`. The `"vectorize"`
  follow-up comes back on the **ingest** queue — queued by the API in
  `routes/broker.py::_advance`, not by the GPU.
- The chat role gets **no S3 at all**. v3's chat worker never touches storage;
  retrieval is a SQL query now (`shared/vectorstore.py`).

Plus the ECS-managed execution role with `secretsmanager:GetSecretValue` on
the secrets each task actually reads — see the note in Phase D about
`BROKER_TOKEN`.

### C3. Migrate — the hard gate (§6.4)

Register a `edgentrag-v3-migrate` task definition (API image, execution role,
`DATABASE_URL` from Secrets Manager), then:

```bash
aws ecs run-task --cluster edgentrag-v3 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnets>],securityGroups=[<edgentrag-v3-ecs>],assignPublicIp=DISABLED}" \
  --task-definition edgentrag-v3-migrate \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","-m","scripts.migrate"]}]}'
```

This applies `0001_initial_schema` then `0002_pgvector`, which runs
`CREATE EXTENSION vector`, adds `chunks.embedding vector(384)`, and builds the
HNSW index.

**Nothing else may start before this succeeds.** No process in v3 creates
schema on startup — that was deliberately removed, because `create_all()` on
every boot becomes a real race the moment many instances and tasks start at
once against shared RDS (`shared/db.py`'s docstring).

**Gate:**

```bash
# CloudWatch should show:  applying migrations up to head  →  done
psql "$DATABASE_URL" -c "\dx"                      # vector extension present
psql "$DATABASE_URL" -c "\d chunks"                # embedding column, ix_chunks_embedding
```

If the task exits instantly with `DATABASE_URL is sqlite; nothing to
migrate`, the task definition's `secrets` block is not wired and the code fell
back to `shared/config.py`'s default.

---

## Phase D — The three programs (parallel)

Units 2, 3 and 4 share nothing but `shared/`. Bring them up in any order, or
all at once.

### Unit 2 — ingest-worker (§6.5–6.7)

The only image carrying Docling, because it is the only one that converts a
document.

| | |
|---|---|
| **Size** | 1 vCPU / 2 GB (raise to 2/4 if OCR on scanned PDFs is slow) |
| **Task role** | `edgentrag-v3-ingest-task-role` |
| **Secrets** | `DATABASE_URL`, `REDIS_URL` |
| **Environment** | `S3_BUCKET`, `AWS_REGION`, `INGEST_QUEUE_URL`, `EMBED_QUEUE_URL`, `STT_QUEUE_URL` |
| **stopTimeout** | 120 (ECS max) — matches the graceful SIGTERM drain in `shared/worker.py` |
| **Networking** | private subnets, `edgentrag-v3-ecs`, no inbound listener |
| **Logs** | `/ecs/edgentrag-v3-ingest` |
| **Autoscale** | target 1–2 msgs/task, min 1, max 3 |

> **Correction to DEPLOY.md §6.5:** it lists `BROKER_TOKEN` among this task's
> secrets. It is not needed. `broker_token` is read only by
> `backend/api/routes/broker.py` (the API, verifying the GPU's bearer header)
> and by the Colab services (`services/*/config.py`, sending it). Neither
> worker references it. Drop it from the task definition and from the ingest
> execution role's `GetSecretValue` resource list — the smaller grant is the
> correct one.

**Why `min = 1` and not 0:** a single upload should never wait on a cold
Fargate start.

**Gate:** service reaches `RUNNING`; CloudWatch shows the long-poll loop with
no `AccessDenied`.

### Unit 3 — chat-worker (§6.5–6.7)

Deliberately the smaller image: no Docling, no S3.

| | |
|---|---|
| **Size** | 0.5 vCPU / 1 GB |
| **Task role** | `edgentrag-v3-chat-task-role` |
| **Secrets** | `DATABASE_URL`, `REDIS_URL` |
| **Environment** | `AWS_REGION`, `CHAT_QUEUE_URL` |
| **stopTimeout** | 60 |
| **Logs** | `/ecs/edgentrag-v3-chat` |
| **Autoscale** | target 1–2 msgs/task, min 1, max 2 |

No service URLs in its environment: it reads the live Colab addresses from
Redis at request time (`shared/services.py`), because a tunnel hostname
changes on every runtime restart.

**Gate:** service `RUNNING`, polling, no `AccessDenied`.

### Unit 4 — api + web on EC2 (§7)

Two containers per instance, from `docker-compose.ec2.yml`. nginx (`web`)
proxies to `api:8000` over the compose network; only 8080 is exposed.

| step | what | gotcha |
|---|---|---|
| 7.1 | instance role `edgentrag-v3-ec2` | needs **no** `sqs:ReceiveMessage` — the API only ever *sends*. Splitting the workers out narrowed this credential. |
| 7.2 | SGs | 8080 from the ALB group only |
| 7.3 | launch template | user data pulls SSM + Secrets, writes `.env`, runs compose |
| 7.4 | target group + ALB | health check **`/api/health`**, **idle timeout 300s** |
| 7.5 | ASG | desired 2, min 2, max 4 |

Three settings here are easy to miss and each has a confusing failure:

- **Health check `/api/health`, not `/api/ready`.** `/health` checks nothing
  external, by design. If the check tested RDS, one brief blip would fail
  every instance's check simultaneously and the ALB would drain all of them —
  turning a recoverable problem into a total outage.
- **ALB idle timeout 300s.** The default 60s silently kills SSE connections.
  This is separate from nginx's own `proxy_read_timeout 24h`, and nothing in
  the app surfaces the mistake except a stream that drops every minute.
- **Min 2, not 1.** Two instances from the start is the entire reason the ALB
  exists — a deploy or an instance failure should never take the app down.

**Gate:**

```bash
curl -s https://<alb-dns>/api/health   # {"status":"ok","service":"api",...}
curl -s https://<alb-dns>/api/ready    # every check true — names the failure if not
```

`/ready` reports database, redis and all four queues **separately**, so a
failure names itself. Use it here, never as the ALB's health check.

---

## Phase E — Edge

**Deploys:** unit 5. **DEPLOY.md:** §9 then §10 (WAF first — it must exist to
appear in CloudFront's dropdown).

WAF: three managed rule groups, plus a rate rule of 2000 req / 5 min per IP,
scoped to **CloudFront**, not the ALB — filtering at the edge blocks traffic
before it reaches the ALB, EC2 or RDS.

CloudFront behaviors:

| path pattern | cache policy | origin request policy |
|---|---|---|
| `/api/sessions/*/events` | CachingDisabled | **AllViewer** |
| `/api/*` | CachingDisabled | **AllViewer** |
| `Default (*)` | CachingOptimized | — |

**`AllViewer` is the single easiest thing to get wrong in this entire
deployment.** CloudFront's default policies strip headers and query strings
before forwarding. Without it:

- `/api/*` loses the `Authorization: Bearer …` header → every authenticated
  call fails at the edge with a 401/403 that has nothing to do with your
  backend
- the events path loses `?ticket=` → SSE 403s immediately

Also set **origin response timeout to 60s** (default 30), for margin above the
app's 15-second SSE heartbeat. The SSE path needs its own behavior entry — it
must exist separately, not folded into `/api/*`.

Then go back and tighten `edgentrag-v3-alb` inbound to CloudFront's managed
prefix list, so nobody can reach the ALB and skip WAF.

**Gate:**

```bash
curl -s https://<cloudfront-domain>/api/health
# Prove Authorization survives the edge — this must be 401 "invalid token",
# NOT 401 "missing bearer token". The second means the header was stripped.
curl -s -H "Authorization: Bearer nonsense" https://<cloudfront-domain>/api/config/services
```

That distinction is the whole test. `missing bearer token` from
`shared/auth.py::_claims` means CloudFront ate the header — go fix
`AllViewer`.

---

## Phase F — Connect the GPU, and verify

**Deploys:** unit 6. **DEPLOY.md:** §11, §6 (services README), §12

The Colab side is last because it needs the public URL, and it connects in
**two independent directions**:

**Them → us (queued work, the broker).** In `services/.env` on Colab:

```ini
BROKER_URL=https://your-domain.example/api
BROKER_TOKEN=<same value as Secrets Manager>
```

The embedding and STT services then poll `/broker/claim` outbound. This half
needs no tunnel at all — every connection starts on Colab and goes out.

**Us → them (interactive calls).** Start the services:

```bash
bash /content/drive/MyDrive/edgentrag_services/run_colab.sh
```

Copy the three printed `trycloudflare.com` addresses into the app's first
screen. `PUT /config/services` validates each one answers `/health` *and* is
the service it claims to be, then stores all three in Redis where every API
task and worker reads them.

This write requires **`admins` group membership** — so sign up, add yourself
to the group in the Cognito console, then sign back in so the new token
carries the claim.

**Final verification (§12):**

1. Open the CloudFront URL → sign up → confirm email code → sign in
2. Paste the three Colab addresses
3. Upload a small `.txt` **and** a short video — they take different paths
4. Network tab: confirm **one long-lived `/events` connection**, not repeated
   `/status` polls
5. Ask a question; the answer should arrive with sources

```bash
# The v3-specific claim: vectors are in Postgres, not just Chroma
psql "$DATABASE_URL" -c "select count(*) from chunks where embedding is not null;"   # > 0

# The broker works from Colab's side (200 with a job or 204 empty — both fine)
curl -s -X POST https://your-domain.example/api/broker/claim \
  -H "Authorization: Bearer $BROKER_TOKEN" -H "Content-Type: application/json" \
  -d '{"job":"embed"}'

curl -s https://your-domain.example/api/metrics/queues | python3 -m json.tool
```

Uploading a video is not optional in step 3 — it is the only path that
exercises STT → broker → `stage:"transcribed"` → embed → `stage:"vectorize"`,
which is the longest chain in the system and the one v3 changed most.

---

## Redeploy matrix

Once live, each unit releases on its own.

| changed | release | downtime |
|---|---|---|
| `workers/ingest/**` or `shared/**` | rebuild + push, `aws ecs update-service --cluster edgentrag-v3 --service ingest-worker --force-new-deployment` | none — new tasks healthy before old ones stop; `shared/worker.py` drains in-flight work on SIGTERM |
| `workers/chat/**` or `shared/**` | same, `--service chat-worker` | none |
| `backend/**` or `shared/**` | `aws autoscaling start-instance-refresh --auto-scaling-group-name edgentrag-v3` | none — ALB drains each instance before replacing it |
| `frontend/**` | rebuild the `web` image → instance refresh | none |
| `VITE_COGNITO_*` | **rebuild required** — baked in at build time | none |
| backend `COGNITO_*` / queue URLs | update SSM → instance refresh | none |
| `migrations/**` | new revision, re-run §6.4's `run-task` **before** deploying code that needs it | none |
| Colab restart | re-run `run_colab.sh`, paste three new addresses | none — addresses live in Redis |

`shared/**` appears three times on purpose: it is copied into all three
images, so a change there means **three** rebuilds. That is the cost of the
top-level `shared/` layout, and it buys independent deployability the rest of
the time.

---

## Pre-flight checklist

Settings with no useful error message if wrong — check each explicitly.

- [ ] ElastiCache **cluster mode disabled** — else SSE silently stops reaching some browsers
- [ ] `DATABASE_URL` starts `postgresql://`, not `postgres://`
- [ ] Migration ran **before** any worker or EC2 instance started
- [ ] `\dx` shows `vector`; `chunks.embedding` and `ix_chunks_embedding` exist
- [ ] ALB health check is `/api/health`, **not** `/api/ready`
- [ ] ALB idle timeout **300s** (default 60 kills SSE)
- [ ] CloudFront `/api/*` **and** the events path both use **AllViewer**
- [ ] CloudFront origin response timeout **60s**
- [ ] Cognito app client has **no client secret**
- [ ] Cognito group named exactly **`admins`**, with you in it
- [ ] `VITE_COGNITO_*` were set **before** the `web` image was built
- [ ] ACM cert for CloudFront is in **us-east-1** (the ALB's is regional — two different certs)
- [ ] RDS and Redis SGs allow **both** `edgentrag-v3-ec2` and `edgentrag-v3-ecs`
- [ ] ASG min is **2**
- [ ] `BROKER_TOKEN` in `services/.env` on Colab matches Secrets Manager exactly

## When something stays stuck

`DEPLOY.md` §13 has the full table. The two most common:

- **A file sits at "processing" forever, no error.** Check the ingest-worker's
  CloudWatch logs first, then the `edgentrag-v3-embed-dlq` depth — five failed
  attempts land there.
- **Every call 401/403 through CloudFront but fine against the ALB directly.**
  `AllViewer` is missing. Phase E.
