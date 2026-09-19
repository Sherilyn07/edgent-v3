# Guide 3: AWS deployment — what we built, step by step, and why

> This guide explains **why** each AWS piece exists. For just the commands, see
> `04-aws-quick-steps.md`. Every step matches one script in `scripts/`, and
> those scripts are what was **actually run** (ap-southeast-1, 2026-09-12).

---

## The big picture

```
                         study.edgent.in  (DNS on Google Cloud)
                                 │
                                 ▼
 Browser ── sign in ──► Cognito
    │
    │ HTTPS
    ▼
 ALB  (+ WAF firewall)                 ← public front door, port 443
    │ port 8080
    ▼
 EC2 instance  (nginx + FastAPI API)   ← "never does slow work"
    │                │         │
    │ send msgs      │ SQL     │ pub/sub
    ▼                ▼         ▼
  SQS queues ──► ECS Fargate workers ──► RDS Postgres (rows + vectors)
  (8 queues)     ingest · chat       └─► ElastiCache Redis
                     │
                     ▼
                  S3 bucket (files)

 Colab GPU ── "any work?" ──► API /broker      (the GPU calls US)
 chat-worker ── direct HTTP ──► Colab /embed_query, /generate
```

**What we planned vs what we got:**

| | Planned (DEPLOY.md) | Actually running |
|---|---|---|
| Region | us-east-1 | **ap-southeast-1** |
| Edge | CloudFront + WAF → ALB | **ALB + WAF** (CloudFront blocked on the new account) |
| EC2 | t4g.medium, 2 to 4 instances | **t2.small, 1 instance** (account quota was 1 vCPU) |
| Network | private subnets + NAT | **default VPC, public subnets**, public IPs, locked by security groups |
| DNS | Route 53 | **Google Cloud DNS** |

## The order, and why it's this order

```
1 Security groups → 2 Database + Redis → 3 S3 → 4 Queues → 5 Cognito
   → 6 Images + IAM + MIGRATE (gate!) → 7 Workers → 8 API servers → 9 HTTPS + DNS
   → 10 Connect Colab + verify
```

- **Firewalls first** (1): everything else attaches to them.
- **Storage next** (2–4): the stateful pieces. Nothing runs yet.
- **Cognito before the API servers** (5): the frontend bakes the Cognito ids in at **build** time.
- **The migration is a hard gate** (6): no program may start before the tables exist.

---

## Step 1 — Security groups (the firewalls)
`scripts/create_security_groups.sh`

### 🟢 Newbie

- **What:** 5 firewalls, one for each kind of thing we run: ALB, EC2, ECS workers, RDS, Redis.
- **Why we need them:** they decide who may talk to whom. For example: "only the API servers and workers may talk to the database".
- **What if we skip it:** either nothing can connect, or everything is open to the internet, including your database.
- **What you get:** 5 group ids (`sg-…`) that every later step uses.

### 🔧 Technical

- Rules point at **other groups**, not at IP addresses. A new instance or task is covered the moment it starts.
- The chain:

  ```
  internet ─443─► alb ─8080─► ec2 ─5432─► rds
                               │  ─6379─► redis
                   ecs ────────┘  (same 5432 / 6379)
  ```

- `edgentrag-v3-ecs` has **zero inbound rules**. The workers pull from SQS, so nothing ever needs to connect *to* them.
- All 5 were created empty first, then the rules were added. This avoids a chicken-and-egg problem, since RDS's rule needs the ECS group to exist.
- There is no SSH port. We use SSM Session Manager when we need a shell.
- Port 80 was added later in Step 8. Without it, `http://` requests hung.

---

## Step 2 — Database (RDS Postgres) and Redis (ElastiCache)
`scripts/create_database_and_cache.sh`

### 🟢 Newbie

- **What:** the two places that remember things.
  - **Postgres:** sessions, files, chunks, messages, **and the vectors**.
  - **Redis:** fast, short-lived things: live progress messages, recent chat turns, the Colab URLs, SSE tickets.
- **Why managed services:** AWS handles backups, patching and restarts for you. v2 used a SQLite file on one server, and losing that server lost everything.
- **What if we skip it:** the app has nowhere to store anything.
- **What you get:** `DATABASE_URL` and `REDIS_URL`, stored safely in Secrets Manager.

### 🔧 Technical

- **RDS settings:**
  - `db.t4g.micro`, Postgres **16.15** (pgvector needs version 15 or newer)
  - 20 GB gp3, single-AZ, 1-day backups
  - **not publicly accessible**
- **Redis settings:**
  - `cache.t4g.micro`, Redis 7.1
  - a **replication group** (needed for TLS), 1 node
- **⚠️ Redis cluster mode must be OFF.** Pub/sub doesn't work reliably across shards, so live progress would silently stop reaching some browsers.
- **⚠️ The URLs have to be exact:**
  - `postgresql://` (not `postgres://`)
  - `rediss://`, with two s's (TLS). Plain `redis://` hangs.
- The password is 32 characters, letters and numbers only (so the URL never needs encoding), and written to a file, never printed.
- Secrets go to Secrets Manager as `edgentrag-v3/database-url` and `edgentrag-v3/redis-url`.
- You **can't** connect to RDS from your laptop. That's correct. The first real test is the migration in Step 6.

---

## Step 3 — S3 bucket (file storage)
`scripts/create_s3_bucket.sh`

### 🟢 Newbie

- **What:** one bucket, `edgentrag-v3-<account-id>`, that holds uploaded files, the extracted text, chunk files and vector files.
- **Why we need it:** large files shouldn't live on a server's disk. S3 is cheap, durable and reachable from anywhere through signed links.
- **What if we skip it:** there is nowhere to upload to.
- **What you get:** a private, encrypted bucket that browsers are allowed to upload to.

### 🔧 Technical

- Public access fully blocked, and SSE-S3 (AES256) encryption turned on.
- **⚠️ The CORS rule is missing from DEPLOY.md, and without it every upload fails:**
  - The rule: `PUT` from any origin, allowed header `Content-Type`.
  - Why it's needed: the browser sends a preflight request to S3, and S3 answers it from this rule.
  - What it looks like when missing: the browser says "upload failed — is the API running?", which is misleading because the API is fine.
- Presigned URLs still work with "block public access" on. A signed request doesn't count as public access.
- SSM parameters `/edgentrag-v3/s3-bucket` and `/edgentrag-v3/aws-region`.

---

## Step 4 — SQS queues (the to-do lists)
`scripts/create_queues.sh` → runs `python -m scripts.bootstrap`

### 🟢 Newbie

- **What:** 4 to-do lists (`ingest`, `chat`, `stt`, `embed`), each with a "failed items" list (a DLQ, dead-letter queue). That's 8 queues in total.
- **Why we need them:** the API writes a note and returns instantly, and the workers pick the notes up. If a worker crashes, the note comes back for someone else to take.
- **What if we skip it:** the API would have to do the slow work itself, and the site would freeze.
- **What you get:** 4 queue URLs plus a freshly generated `BROKER_TOKEN` (the GPU's password).

### 🔧 Technical

- It uses the repo's own `bootstrap.py`, so the queue settings come from `shared/config.py`. The queue's max retries (5) and the broker's "last attempt" check read **the same value**.
- Queue settings:
  - visibility 900 s
  - long-poll 20 s
  - retention 4 days
  - `maxReceiveCount 5` → after 5 failures, the message moves to the DLQ
- **⚠️ Set `AWS_REGION` in `.env` first.** Otherwise the queues are silently created in us-east-1.
- `BROKER_TOKEN` is printed **once**, so save it right away:
  - it goes to Secrets Manager as `edgentrag-v3/broker-token`
  - the queue URLs go to SSM as `/edgentrag-v3/{ingest,chat,stt,embed}-queue-url`

---

## Step 5 — Cognito (user sign-in)
`scripts/create_cognito.sh`

### 🟢 Newbie

- **What:** AWS's ready-made login system, covering sign-up, the email code, passwords and tokens.
- **Why we need it:** so each user sees **only their own** sessions. In v2, anyone holding a session id could read it.
- **What if we skip it:** the app runs with no login at all. That's fine on a laptop, and dangerous on the internet.
- **Why before the servers:** the website bakes the Cognito ids into its JavaScript **when it is built**. Doing this first means you build once.
- **What you get:** a user pool id, a client id, and an `admins` group.

### 🔧 Technical

- **Pool:** email as the username, email verification, 8-character passwords with no symbols required, self sign-up on.
- **App client:**
  - `--no-generate-secret` (**required**: a browser can't keep a secret)
  - SRP and refresh auth flows only
  - 1 h ID token, 30-day refresh token
- **The `admins` group:** only admins can `PUT /config/services` (change the Colab URLs).
- **The ids go to 2 places:**
  - SSM `/edgentrag-v3/cognito-*`, which the API reads at start
  - `frontend/.env.production` `VITE_COGNITO_*`, which is baked in at build
- **Check it worked:** the JWKS URL returns 2 RS256 keys. The API uses them to verify tokens.

---

## Step 6 — Images, permissions, and the database migration
`scripts/create_images_and_migrate.sh`

### 🟢 Newbie

- **What:** four things happen here:
  - package the code into 3 Docker images (api, ingest-worker, chat-worker) and upload them to ECR (AWS's image store)
  - create the permissions each program runs with
  - create the ECS cluster
  - **create the database tables**
- **Why we need it:** AWS runs images, not source code. Each program should get only the permissions it needs.
- **Why the migration is a gate:** if a worker starts before the tables exist, it crashes. If the API starts first, every request fails.
- **What if we skip the migration:** nothing works. It shows up as "table does not exist" errors.
- **What you get:** images in ECR, 3 IAM roles, and tables + pgvector in Postgres.

### 🔧 Technical

- **Images built for ARM64** (`--platform linux/arm64`): native on a Mac and about 20% cheaper on Fargate Graviton. The task definitions **must** say `cpuArchitecture: ARM64`.
- Build from the **repo root** (`-f workers/x/Dockerfile .`) so `shared/` gets copied in.
- **Two kinds of role:**
  - The **execution role** is used by ECS itself: it pulls the image, reads secrets and writes logs.
  - The **task role** is used by your code at runtime (the boto3 calls).
- **What each task role allows:**
  - **ingest:** receive/delete on `ingest` · send to `stt` + `embed` · get/put on the bucket
  - **chat:** receive/delete on `chat` **only**. No S3 at all.
  - **execution:** read `database-url` and `redis-url`. Not the broker token, since no worker uses it.
- **The migration** is a one-off Fargate task that uses the api image to run `python -m scripts.migrate`:
  - `0001` creates the tables.
  - `0002` does `CREATE EXTENSION vector`, adds `embedding vector(384)` and builds the HNSW index.
- `assignPublicIp=ENABLED`, because there's no NAT gateway and the task needs a way out to pull its image.
- `set -o pipefail` matters: a failed `docker push | tail` once looked like a success.

---

## Step 7 — The worker services (ECS Fargate)
`scripts/create_worker_services.sh`

### 🟢 Newbie

- **What:** two programs that run all the time: the **ingest worker** (processes files) and the **chat worker** (answers questions).
- **Why Fargate:** AWS runs the containers for you, with no servers to manage. It adds more copies when the queue gets long.
- **Why two separate services:** the ingest image is 3.4 GB and the chat image is 116 MB. A person waiting for an answer shouldn't wait on a 3.4 GB startup.
- **What if we skip it:** uploads stay "pending" and questions are never answered.
- **What you get:** 2 services with at least 1 task each, scaling up when busy.

### 🔧 Technical

| | ingest | chat |
|---|---|---|
| CPU / memory | 1 vCPU / 2 GB | 0.5 vCPU / 1 GB |
| Disk | 30 GB (Docling models + video) | default |
| stopTimeout | 120 s (lets the current job finish) | 60 s |
| Environment | bucket + ingest, stt and embed queues | chat queue only |
| Autoscale | min 1, max 3 | min 1, max 2 |

- Secrets (`DATABASE_URL`, `REDIS_URL`) come in through the `secrets` field, never `environment`, which would expose the password in the console.
- **Autoscaling uses "backlog per task"** = queue messages ÷ running tasks, with a target of 2.
  - Container Insights has to be on for this.
  - Scale out after 60 s, scale in after 300 s.
- **⚠️ zsh bug:** `$ACC:edgentrag` breaks the ARN in zsh. Write `${ACC}`.
- **What healthy logs look like:** 2 lines, then silence while the worker waits for work:

  ```
  database_url is not sqlite; schema is managed by Alembic
  ingest: consuming edgentrag-v3-ingest
  ```

- An IAM fix takes effect on a running task. An env or secret change needs `--force-new-deployment`.

---

## Step 8 — The API servers (ALB + EC2 Auto Scaling Group)
`scripts/create_alb_and_asg.sh`

### 🟢 Newbie

- **What:**
  - A **load balancer (ALB)**: the public front door that handles HTTPS.
  - Behind it, **EC2 servers** running the website (nginx) and the API (FastAPI).
  - An **Auto Scaling Group**, which replaces a server if it dies.
- **Why we need it:** this is what your browser actually talks to.
- **What if we skip it:** there's no website and no API. Nothing is reachable.
- **What you get:** `https://<alb-dns>/api/health` → `{"status":"ok"}`.

### 🔧 Technical

- **The instance role and profile** (EC2 needs a *profile* that wraps the role). It allows:
  - SQS and S3
  - reading the 3 secrets, including **the broker token**, which only the API needs
  - SSM parameters
  - Session Manager
- **Target group:** port 8080, health check **`/api/health`** (never `/ready`, which checks the database: one database hiccup would pull every server out at once).
- **ALB:**
  - listener 443 with an ACM certificate, TLS 1.3 policy
  - port 80 → 301 redirect to HTTPS
  - **idle timeout 300 s** (the default 60 s silently cuts live streams)
- **Launch template:**
  - Ubuntu 24.04, **t2.small**, 30 GB disk
  - **IMDS hop limit 2** (otherwise the container gets `NoCredentialsError`)
- **User data** (a script that runs at first boot):
  1. adds 4 GB of swap (a 2 GB box otherwise gets out-of-memory killed during `vite build` → exit 137)
  2. installs Docker
  3. `git clone`s the repo
  4. builds `.env` from SSM and Secrets Manager
  5. runs `docker compose -f docker-compose.ec2.yml up -d --build`
- **ASG:** health type **ELB**, a **600 s grace period** (the build is slow on one vCPU), min/max/desired **1/1/1** because of the quota. The plan was 2/2/4.
- **⚠️ Quota trap:** `--dry-run` said OK but real launches failed. The error only shows in `aws autoscaling describe-scaling-activities`.

---

## Step 9 — HTTPS certificates, WAF, CloudFront and DNS
`scripts/create_edge_and_dns.sh`

### 🟢 Newbie

- **What:**
  - **Certificates** so the site works over `https://`.
  - A **WAF** (web firewall) that blocks known attacks and floods.
  - A **DNS record** so `study.edgent.in` points at our load balancer.
- **Why we need it:** browsers need HTTPS, and people need a name instead of `edgentrag-v3-12345.elb.amazonaws.com`.
- **What if we skip it:** you can only use the ugly AWS address, and there's no attack protection.
- **What you get:** `https://study.edgent.in` works.

### 🔧 Technical

- **2 ACM certificates for 1 name:**
  - one in **us-east-1** (CloudFront requires it)
  - one in ap-southeast-1 (for the ALB)
  - One DNS CNAME validates both.
- **2 WAF web ACLs:**
  - `CLOUDFRONT` scope, in us-east-1
  - `REGIONAL` scope, attached to the ALB ← **this is the one in use**
- **The WAF rules:**
  - AWS Common, Known Bad Inputs, IP Reputation
  - a **rate limit of 2000 requests per 5 minutes per IP**
- **CloudFront was blocked:** `AccessDenied … account must be verified`. It needs an AWS Support case. The config is ready:
  - `/api/sessions/*/events` → CachingDisabled + **AllViewer**
  - `/api/*` → CachingDisabled + **AllViewer**
  - default → CachingOptimized
  - origin timeout 60 s
- **⚠️ AllViewer** is what forwards the `Authorization` header and `?ticket=`. Without it you get 401s and 403s at the edge.
- **DNS:** CNAME `study.edgent.in` → the ALB DNS name, in Google Cloud DNS, TTL 300. It's a subdomain because DNS doesn't allow a CNAME at the apex.
- **The header test:**
  - `curl -H "Authorization: Bearer nonsense" …/api/config/services` should return `"unreadable token"`. That's good: the header got through.
  - `"missing bearer token"` means something in front of the API stripped the header.

---

## Step 10 — Connect the Colab GPU and check it all works
`services/README.md` + a few manual steps

### 🟢 Newbie

- **What:** start the 3 AI services on Colab, tell the app where they are, then try it.
- **Why we need it:** without the GPU there are no vectors, no transcripts and no answers.
- **What you get:** a working app.

### 🔧 Technical

1. **Create your admin account:**
   - Sign up at `https://study.edgent.in` and enter the emailed code.
   - Run `aws cognito-idp admin-add-user-to-group … --group-name admins`.
   - **Sign out and back in.** The group lives inside the token, and your old token doesn't have it.
2. **Point Colab at us.** In `services/.env` on Colab:
   - `BROKER_URL=https://study.edgent.in/api`
   - `BROKER_TOKEN=<same value as Secrets Manager>`
3. **Start the services:** `bash run_colab.sh`. It prints 3 `trycloudflare.com` URLs. Paste them into the app's first screen, which saves them in Redis.
4. **Test:**
   - Upload a `.txt` **and** a short video. The video path is the longest chain.
   - Check there's one long-lived `/events` connection in the browser's Network tab.
   - Ask a question and check the sources.
5. **Prove the vectors reached Postgres:** `select count(*) from chunks where embedding is not null;` should be > 0.

---

## Monthly cost, roughly (with nothing running)

| Item | Cost |
|---|---|
| ALB | ~$16–20/month, even with no traffic |
| RDS + ElastiCache | the other fixed cost |
| WAF | $5/month per ACL + ~$1/month per rule (delete the unused CloudFront one) |
| Fargate | per second, at least 1 task each |
| ACM certificates | free |

**To pause:**

- Set the ASG to 0.
- Set both ECS services to desired count 0.
- The ALB, RDS and Redis keep billing until you delete them.
