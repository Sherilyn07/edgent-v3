# EdgentRAG v3

The version that survives more than one machine. Version 2 rewrote the code
around one rule — the API never does slow work — but kept version 1's
deployment: one EC2 box, one SQLite file, one Redis with no persistence. This
version keeps the rule and replaces the deployment: managed AWS services
underneath, so the app can lose a process, an instance, or a GPU runtime
without losing data, and can grow past what one box can hold.

`DEPLOY.md` is the step-by-step manual runbook — you provision the AWS
infrastructure by hand, following it. `../v2/TUTORIAL.md` and
`../v2/V3_DESIGN.html` explain *why* each piece exists; this file is what
changed in the code to get there.

---

## What changed from version 2

| | v2 | v3 |
|---|---|---|
| Relational data | SQLite, one file, WAL mode | RDS PostgreSQL |
| Vectors | Chroma, inside the embedding service | pgvector, in that same RDS Postgres |
| Cache / pub-sub | Redis in a container, no persistence | ElastiCache Redis |
| Ingest / chat workers | one Docker image, two `command:` variants, containers on the EC2 box | two separately built, separately deployed **ECS Fargate services** |
| API | FastAPI + nginx, one EC2 box, Cloudflare tunnel | FastAPI + nginx, an EC2 **Auto Scaling Group** behind an **ALB** |
| Edge | the tunnel, directly | **CloudFront** (+ **WAF**) in front of the ALB |
| Identity | none — a session id was the only "identity" | **Cognito**: every session has an owner, every route checks it |
| AI services (Colab) | broker pattern, `/retrieve` + `/generate` direct calls | same broker pattern; `/retrieve` retired from the production path, a small `/embed_query` endpoint added; no model or optimization changes |

### What did not change

The prompt assembly, the chunking strategy, the presigned-upload pattern, the
broker pattern that lets the GPU work without AWS credentials, and almost all
of the frontend. Version 2 already put the *code* in the right shape; v3
changes what it runs on, not how it thinks.

---

## What is here

```
shared/          config, models (+pgvector), db, storage, queues, events,
                 services, clients, vectorstore (new), chunking, convert,
                 bookkeeping, schemas, auth (new), worker
                 -- one package, copied unmodified into every image below
backend/         the API: FastAPI, routes, Dockerfile. Runs on EC2.
workers/
  ingest/        its own image, its own ECS Fargate service. Docling lives
                 only here.
  chat/          its own image, its own ECS Fargate service. Smaller: no
                 Docling, no S3 access — retrieval is a database query now.
frontend/        the React app, plus Cognito sign-in (auth.js, Login.jsx)
services/        the three AI services, run on Colab -- unchanged except a
                 small addition to the embedding service (embed_query, and
                 writing vectors out alongside Chroma)
migrations/      Alembic — the schema, and the pgvector step, as two
                 explicit, ordered migrations
docker/          nginx.conf (unchanged from v2)
scripts/         bootstrap.py (SQS queues, edgentrag-v3-* prefix),
                 migrate.py (the one-off "apply migrations" step)
docker-compose.yml       local dev: SQLite + local Redis, four containers
docker-compose.ec2.yml   what actually runs on an EC2 instance: web + api
DEPLOY.md        the manual AWS runbook
```

Why `shared/` is a top-level directory and not nested under `backend/`: three
images are built from it now (`backend/Dockerfile`,
`workers/ingest/Dockerfile`, `workers/chat/Dockerfile`), each with `COPY
shared /app/shared` alongside its own code. That is the whole mechanism that
lets the workers be a fully separate deployable service — mirroring how
`services/` (the AI side) has always been separate from the backend — without
duplicating a line of the shared code three times.

---

## Running it locally (no AWS needed)

```bash
cp .env.example .env             # fill in S3_BUCKET and the four queue URLs
python -m scripts.bootstrap      # once, prints the queue URLs (needs AWS creds)
docker compose up -d --build
make ready
```

This runs against local SQLite and a local Redis — the same "does the code
work" loop version 2 offered, with no Cognito gate (leave the `COGNITO_*`
variables blank) and no pgvector (SQLite has no vector type; the "vectorize"
stage is simply never exercised locally). It is enough to validate everything
except the parts that only exist in AWS: RDS, ElastiCache, ECS autoscaling,
Cognito, CloudFront, WAF.

Point it at the three Colab services exactly as in version 2 — paste their
addresses into the app's first screen.

## Deploying for real

`DEPLOY.md`, start to finish. In short: RDS Postgres (with pgvector) and
ElastiCache Redis for state; an ECS cluster running the two worker services,
autoscaled on queue depth; an EC2 Auto Scaling Group behind an ALB for the
API and frontend; CloudFront and WAF in front of that; Cognito for identity.
The same AWS account, region and S3 bucket as v1/v2 — nothing there needs
recreating.

---

## Things worth knowing

**The broker is still a separate credential system from Cognito, on
purpose.** The broker's bearer token authenticates a machine — the Colab GPU
— that can never hold an AWS or Cognito identity, and it grants exactly one
thing: "ask for a job." Cognito authenticates a person and grants "see your
own sessions." Merging the two would be a mistake even though both live in
`shared/`.

**A native `EventSource` cannot carry a Cognito token.** The event stream is
authenticated with a short-lived *ticket* instead, minted through an
ordinary, authenticated call and passed as a query parameter — not the JWT
itself, and not single-use, so the browser's automatic reconnect keeps
working. See `backend/api/routes/tickets.py`.

**Vectors are written twice, for now.** The embedding service still writes
into Chroma (as it always has) *and* streams its computed vectors out to S3
for the ingest worker to load into Postgres. Chroma is unused by the
production query path as of this version — retrieval goes through
`shared/vectorstore.py` — and is kept only as a working fallback during the
transition. Removing it is a deliberately separate, later change.

**Schema changes are a migration, not a process startup.** `init_db()`
still exists, but only creates tables against local SQLite for laptop
convenience. Against Postgres, run `python -m scripts.migrate` once, as its
own step — never from the API or a worker's own startup, which would race
across every EC2 instance and ECS task starting at once.

**SQS queues are versioned, not shared with v2.** `edgentrag-v3-*`, not
`edgentrag-*`. The `embed` job's message gained a field and the `ingest`
queue gained a new stage that a version 2 worker does not understand —
separate queues make that a non-issue rather than something to sequence
carefully.

---

## What v3 still does not solve

**The GPU is still the ceiling.** One Colab runtime, no autoscaling, by
design for this course — see `../v2/V3_DESIGN.html`'s Part 7 for the full
argument. Every piece of AWS infrastructure here is sized to scale *up to*
that ceiling, not past it.

**Retrieval quality is untouched.** Chunking, prompt assembly and the model
itself are identical to version 2. Moving the vectors into Postgres changes
*where* they live, not *how good* a match they find.

**Cost now scales with the infrastructure, not just usage.** RDS,
ElastiCache, an ALB, NAT (if the workers' subnets need one for outbound
Colab/model-service calls) and CloudFront have a floor above zero before a
single user arrives. `DEPLOY.md` calls out sizing choices that keep that
floor low for a course project.
