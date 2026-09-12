#!/usr/bin/env bash
#
# Task 6 of the EdgentRAG v3 deployment — ECR images, IAM task roles, and the
# schema migration.
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# THIS IS THE FIRST TASK WHERE CODE ACTUALLY RUNS IN AWS.
# Tasks 1-5 provisioned resources. This one builds the three images, grants the
# permissions they will run with, and executes the migration that every other
# component depends on.
#
# THE MIGRATION IS A HARD GATE
# ----------------------------
# No process in v3 creates schema at start-up. Version 2 ran create_all() on
# every API and worker boot, which is harmless with one SQLite file and a real
# race the moment many EC2 instances and ECS tasks start at once against shared
# RDS. So shared/db.py's init_db() now only does anything against SQLite, and
# Postgres schema is Alembic's job — run ONCE, as its own step.
#
# Start a worker before this migration succeeds and it crashes on a missing
# table. Start the API and every request 500s. Run it first.
#
# HOW TO RUN
#   bash scripts/create_images_and_migrate.sh

set -euo pipefail

export AWS_REGION=ap-southeast-1
ACC=$(aws sts get-caller-identity --query Account --output text)
REG=$ACC.dkr.ecr.$AWS_REGION.amazonaws.com

# ---------------------------------------------------------------------------
# Step 1 — ECR repositories
# ---------------------------------------------------------------------------
# Three repositories, because three images are built from this one repo and
# deployed independently. That independence is the whole reason shared/ is a
# top-level directory rather than living under backend/ — each Dockerfile does
# `COPY shared /app/shared` from the repo root, so all three get the same code
# without a published package and without duplicating a line.
#
# scanOnPush finds known CVEs in the image's OS packages for free. Worth
# turning on everywhere; it costs nothing and occasionally matters.
for r in api ingest-worker chat-worker; do
  aws ecr create-repository --region "$AWS_REGION" --repository-name "edgentrag-v3/$r" \
    --image-scanning-configuration scanOnPush=true \
    --tags Key=Project,Value=edgentrag-v3
done

# ECR login. The token is valid 12 hours; re-run this when a push suddenly
# 401s after lunch.
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REG"

# ---------------------------------------------------------------------------
# Step 2 — build, and the architecture decision
# ---------------------------------------------------------------------------
# THE THING TO UNDERSTAND HERE: this laptop is Apple Silicon (`uname -m` says
# arm64). Docker builds for the host architecture by default, so these images
# are ARM64 — and a task definition that does not say so will try to run them
# on X86_64 Fargate and fail with:
#
#     image Manifest does not contain descriptor matching platform 'linux/amd64'
#
# Two ways out, and this is a genuine choice:
#
#   a) --platform linux/amd64
#      Cross-compiles under QEMU emulation. Correct, and SLOW — the ingest
#      image compiles a large native dependency tree (Docling) and emulation
#      can turn minutes into the better part of an hour.
#
#   b) build ARM64 natively, tell Fargate to run ARM64.        <- what we did
#      Native speed, and Graviton Fargate is roughly 20% cheaper than x86 for
#      the same work. The cost is that the task definition MUST carry
#      runtimePlatform.cpuArchitecture = ARM64 (see step 5).
#
# --platform linux/arm64 is written explicitly rather than relying on the
# default, so the same command produces the same image on an Intel machine.
# A build whose output depends on who ran it is a bug waiting to happen.
#
# Note the build context is `.` (the repo root) for all three, with -f
# pointing at the Dockerfile. Build from inside workers/ingest/ and the COPY
# of shared/ fails — that directory is not in the context.
docker build --platform linux/arm64 -f backend/Dockerfile        -t "$REG/edgentrag-v3/api:latest"           .
docker build --platform linux/arm64 -f workers/chat/Dockerfile   -t "$REG/edgentrag-v3/chat-worker:latest"   .
docker build --platform linux/arm64 -f workers/ingest/Dockerfile -t "$REG/edgentrag-v3/ingest-worker:latest" .

# Push each image AS SOON AS it is built, not all three at the end. Ordering
# build,build,build,push,push,push leaves two finished 500 MB images idle for
# the ~10 minutes the 3.6 GB ingest build takes, for no reason.
docker push "$REG/edgentrag-v3/api:latest"
docker push "$REG/edgentrag-v3/chat-worker:latest"
docker push "$REG/edgentrag-v3/ingest-worker:latest"

# A REAL FAILURE WE HIT HERE, worth showing students deliberately.
#
# The first attempt at this step ran the pushes through a pipe to trim output:
#
#     docker push ... 2>&1 | tail -3
#
# The push failed — the Mac's disk was full, and containerd could not write the
# blob. But a pipeline's exit status is the LAST command's, so the shell saw
# tail's happy 0, `set -e` did not fire, and the script printed
# "ALL BUILDS AND PUSHES DONE" over a hard failure. ECR stayed empty.
#
# `set -o pipefail` at the top of this file is exactly what prevents that: it
# makes a pipeline fail if ANY stage fails. The same mistake in a CI config
# turns a broken deploy into a green build, which is how it usually gets
# discovered — much later, and expensively.
#
# Always verify the far end rather than trusting the exit code. ECR is the
# authority on whether a push happened, not the script that ran it:
for r in api chat-worker ingest-worker; do
  aws ecr describe-images --region "$AWS_REGION" --repository-name "edgentrag-v3/$r" \
    --query 'imageDetails[0].[imageTags[0],imageSizeInBytes]' --output text
done

# DISK: the ingest image is ~3.6 GB and Docker Desktop stores everything in a
# sparse Docker.raw that only ever grows. Check `df -h` BEFORE building; a full
# disk surfaces as an opaque containerd "input/output error", not as
# "no space left on device". `docker system prune -af` reclaims Docker's share.

# Compare the sizes afterwards. chat-worker is meaningfully smaller than
# ingest-worker, and that is by design, not accident: chat carries no Docling
# because it never converts a document, and touches no S3 because retrieval is
# a SQL query now. Splitting one worker image into two made the interactive
# path's cold start faster and its attack surface smaller.
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep edgentrag

# ---------------------------------------------------------------------------
# Step 3 — the ECS cluster
# ---------------------------------------------------------------------------
# A cluster on Fargate is just a namespace — no servers, nothing to size.
#
# FIRST-TIME-IN-AN-ACCOUNT GOTCHA: this can fail with
#     "Unable to assume the service linked role"
# ECS needs AWSServiceRoleForECS, created automatically the first time you use
# ECS in the console but not necessarily via the CLI. Create it explicitly:
#
#     aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com
#
# If that answers "has been taken in this account", the role exists and the
# original error was propagation lag — wait a few seconds and retry.
aws ecs create-cluster --region "$AWS_REGION" --cluster-name edgentrag-v3 \
  --capacity-providers FARGATE --tags key=Project,value=edgentrag-v3

# ---------------------------------------------------------------------------
# Step 4 — IAM: three roles, and the difference between two kinds
# ---------------------------------------------------------------------------
# THIS DISTINCTION CONFUSES EVERYONE ONCE:
#
#   execution role  used by the ECS AGENT, before your code runs. Pulls the
#                   image from ECR, fetches secrets, writes to CloudWatch Logs.
#   task role       used by YOUR CODE, at runtime. This is what boto3 inside
#                   the container picks up.
#
# Symptom of confusing them: the task fails to start at all (execution role is
# wrong) versus the task starts fine and then gets AccessDenied on its first
# SQS call (task role is wrong).
cat > /tmp/ecs-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

for r in edgentrag-v3-ingest-task-role edgentrag-v3-chat-task-role edgentrag-v3-execution-role; do
  aws iam create-role --role-name "$r" \
    --assume-role-policy-document file:///tmp/ecs-trust.json \
    --tags Key=Project,Value=edgentrag-v3
done

# BRACES ARE LOAD-BEARING HERE. Written as `$ACC:edgentrag-v3`, zsh reads the
# ":e" as a history modifier meaning "file extension", expands $ACC:e to the
# empty string (${ACC} has no dot), and swallows the "e" — producing
#     arn:aws:sqs:ap-southeast-1:dgentrag-v3
# which IAM accepts happily as a valid-looking ARN for a queue that does not
# exist. bash has no such modifier and the unbraced version works there, which
# is what makes this the classic "works on my machine" bug. See
# scripts/create_worker_services.sh for the full write-up and the symptom.
Q="arn:aws:sqs:${AWS_REGION}:${ACC}:edgentrag-v3"
BUCKET="arn:aws:s3:::edgentrag-v3-${ACC}"

# The ingest worker's permissions, read straight off workers/ingest/main.py:
#   ConsumeIngest      the Worker loop polling the ingest queue
#   QueueFollowUpJobs  _request_transcription sends an stt job;
#                      _store_and_queue sends an embed job
#   Bucket             download the raw file, upload text/chunks, read vectors
#
# NOTE WHAT IS ABSENT: no ReceiveMessage on embed. It looks like it should
# need it — it sends embed jobs, and a "vectorize" message comes back later.
# But that message is queued onto the INGEST queue by the API
# (routes/broker.py::_advance), not onto embed. Tracing the actual message
# flow gives a smaller grant than guessing from the names.
cat > /tmp/ingest-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"ConsumeIngest","Effect":"Allow",
  "Action":["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:ChangeMessageVisibility","sqs:GetQueueAttributes"],
  "Resource":"${Q}-ingest"},
 {"Sid":"QueueFollowUpJobs","Effect":"Allow","Action":"sqs:SendMessage",
  "Resource":["${Q}-stt","${Q}-embed"]},
 {"Sid":"Bucket","Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],
  "Resource":"${BUCKET}/*"}]}
EOF

# The chat worker's, and it is worth pausing on how SHORT it is: one queue,
# nothing else. No S3 at all. In v2 this worker called the embedding service's
# /retrieve over HTTP and then fetched text separately; in v3 retrieval is one
# SQL query (shared/vectorstore.py::search), so the entire storage permission
# disappeared. An architecture change that shrinks an IAM policy is usually a
# good sign.
cat > /tmp/chat-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"ConsumeChat","Effect":"Allow",
  "Action":["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:ChangeMessageVisibility","sqs:GetQueueAttributes"],
  "Resource":"${Q}-chat"}]}
EOF

aws iam put-role-policy --role-name edgentrag-v3-ingest-task-role \
  --policy-name ingest-worker --policy-document file:///tmp/ingest-policy.json
aws iam put-role-policy --role-name edgentrag-v3-chat-task-role \
  --policy-name chat-worker --policy-document file:///tmp/chat-policy.json

# Neither task role mentions Postgres or Redis. Those are reached over the
# network and authenticated by password, gated by the security groups from
# task 1 — not by IAM. Two different mechanisms guarding two different things,
# and a common point of confusion.

# The execution role: the AWS-managed policy covers ECR pull + CloudWatch Logs.
aws iam attach-role-policy --role-name edgentrag-v3-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# Plus the two secrets these tasks inject. DEPLOY.md section 6.3 lists three,
# including broker-token — that one is unnecessary. Grep for it:
# settings.broker_token is read only by backend/api/routes/broker.py (the API
# verifying the GPU's bearer header) and by services/*/config.py on Colab.
# Neither worker, and not the migration task, ever reads it.
DB_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/database-url --query ARN --output text)
RD_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/redis-url --query ARN --output text)
cat > /tmp/exec-secrets.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"secretsmanager:GetSecretValue",
  "Resource":["$DB_ARN","$RD_ARN"]}]}
EOF
aws iam put-role-policy --role-name edgentrag-v3-execution-role \
  --policy-name read-deployment-secrets --policy-document file:///tmp/exec-secrets.json

# Log groups, created up front. ECS can create them itself only with extra
# permission; making them explicitly means a task that fails instantly still
# has somewhere to have written why.
for g in migrate ingest chat; do
  aws logs create-log-group --region "$AWS_REGION" --log-group-name "/ecs/edgentrag-v3-$g"
done

# ---------------------------------------------------------------------------
# Step 5 — the migration task definition
# ---------------------------------------------------------------------------
# Built from the API image, which already bundles migrations/ and alembic
# (see backend/Dockerfile's COPY of scripts/ and migrations/). No separate
# image, no new infrastructure — it runs in the same subnets and security
# group as the workers, so it can reach RDS.
#
# Three fields worth understanding:
#
#   runtimePlatform ARM64   MUST match what step 2 built. This is the other
#                           half of the architecture decision. Get it wrong and
#                           the task fails to start with a manifest error.
#   networkMode awsvpc      mandatory on Fargate: the task gets its own ENI and
#                           its own private IP, which is what lets a SECURITY
#                           GROUP apply to it — that is how $SG_ECS reaches RDS.
#   secrets (not environment)  DATABASE_URL is injected from Secrets Manager at
#                           start. Putting it in `environment` would write the
#                           password into the task definition JSON, which is
#                           readable by anyone with console access and kept
#                           forever as a revision.
#
# The container is named "api" so DEPLOY.md's --overrides containerOverrides
# example (which names "api") works unmodified.
cat > /tmp/migrate-taskdef.json <<EOF
{
  "family": "edgentrag-v3-migrate",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::${ACC}:role/edgentrag-v3-execution-role",
  "containerDefinitions": [{
    "name": "api",
    "image": "${REG}/edgentrag-v3/api:latest",
    "essential": true,
    "command": ["python", "-m", "scripts.migrate"],
    "secrets": [{"name": "DATABASE_URL", "valueFrom": "${DB_ARN}"}],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/edgentrag-v3-migrate",
        "awslogs-region": "${AWS_REGION}",
        "awslogs-stream-prefix": "migrate"
      }
    }
  }]
}
EOF
aws ecs register-task-definition --region "$AWS_REGION" --cli-input-json file:///tmp/migrate-taskdef.json

# ---------------------------------------------------------------------------
# Step 6 — run the migration
# ---------------------------------------------------------------------------
# assignPublicIp=ENABLED is required HERE, and it is worth explaining rather
# than copying. The task must pull its image from ECR, which is a public
# endpoint. Our subnets are public but have no NAT gateway, so a task with no
# public IP has no route out and the pull times out after several minutes with
# a misleading CannotPullContainerError.
#
# Two legitimate fixes: give the task a public IP (free, what we do here), or
# add VPC endpoints for ECR/S3/Secrets Manager/CloudWatch so the traffic never
# leaves AWS (more secure, costs per endpoint per hour). DEPLOY.md assumes
# private subnets with a NAT gateway, which is a third answer with its own
# monthly cost. For a course project the public IP is the honest choice —
# the security group still allows nothing inbound.
aws ecs run-task --region "$AWS_REGION" \
  --cluster edgentrag-v3 \
  --launch-type FARGATE \
  --task-definition edgentrag-v3-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0a466fb1f2d3a1e8f,subnet-06ecb1c97f87a5545,subnet-0ac3b9285a4d3e530],securityGroups=[sg-029616add5e208bbc],assignPublicIp=ENABLED}"

# Watch it. Expect, in CloudWatch:
#     INFO  applying migrations up to head
#     INFO  done
#
# If instead it prints
#     DATABASE_URL is sqlite; nothing to migrate
# then the secret did not inject and shared/config.py's default loaded. Check
# the `secrets` block, not `environment`.
aws logs tail /ecs/edgentrag-v3-migrate --region "$AWS_REGION" --since 10m

# ---------------------------------------------------------------------------
# Step 7 — verify the schema
# ---------------------------------------------------------------------------
# You cannot psql from a laptop — RDS has no public endpoint and its security
# group admits only the EC2 and ECS groups. That is correct, not an obstacle.
# Check from inside instead, by running a one-off task with a different
# command against the same image.
#
# Expect: the `vector` extension present, and chunks carrying both an
# `embedding` column and the `ix_chunks_embedding` HNSW index — migration
# 0002_pgvector's three statements.
#
#   aws ecs run-task --region $AWS_REGION --cluster edgentrag-v3 --launch-type FARGATE \
#     --task-definition edgentrag-v3-migrate \
#     --network-configuration "awsvpcConfiguration={subnets=[...],securityGroups=[sg-029616add5e208bbc],assignPublicIp=ENABLED}" \
#     --overrides '{"containerOverrides":[{"name":"api","command":["python","-c",
#       "import sqlalchemy as sa, os; e=sa.create_engine(os.environ[\"DATABASE_URL\"]);
#        c=e.connect();
#        print(c.execute(sa.text(\"select extname from pg_extension\")).fetchall());
#        print(c.execute(sa.text(\"select tablename from pg_tables where schemaname=(quote)public(quote)\")).fetchall())"]}]}'
#
# ---------------------------------------------------------------------------
# REDEPLOYING LATER
# ---------------------------------------------------------------------------
# A code change in shared/ means THREE rebuilds and three pushes, because all
# three images copy it. That is the cost of the top-level shared/ layout, and
# it buys independent deployability the rest of the time.
#
# A new migration: add the Alembic revision, rebuild and push the API image,
# then re-run step 6 BEFORE deploying code that depends on the new schema.
#
# ---------------------------------------------------------------------------
# NEXT: Phase D — the two ECS worker services (task definitions, services,
# autoscaling on queue depth), and the EC2 Auto Scaling Group behind the ALB.
# Those three are independent of each other and can be done in any order.
