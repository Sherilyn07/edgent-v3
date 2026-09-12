#!/usr/bin/env bash
#
# Task 4 of the EdgentRAG v3 deployment — the eight SQS queues.
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# WHAT IS DIFFERENT ABOUT THIS TASK
# ---------------------------------
# Tasks 1-3 were raw AWS CLI. This one is not: the repo ships its own
# provisioning script, scripts/bootstrap.py, and we run THAT.
#
# That is a deliberate teaching point. Look at what bootstrap.py reads:
#
#     MAX_RECEIVES = settings.queue_max_receives     # shared/config.py
#     "VisibilityTimeout":  settings.queue_visibility_seconds
#     "ReceiveMessageWaitTimeSeconds": settings.queue_wait_seconds
#
# The same Settings object the running application reads. So maxReceiveCount
# on the queue and the broker's notion of "this was the last attempt"
# (api/routes/broker.py: MAX_RECEIVES = settings.queue_max_receives) are
# guaranteed to agree, because they are literally one value with two readers.
#
# Hand-write these queues with `aws sqs create-queue` and you have introduced
# a number that must be kept in sync with application code by memory. When it
# drifts, the broker gives up at attempt 5 while the queue retries to 10 — or
# worse, the reverse, and a file sits at "processing" forever with no error
# because nothing ever decides it has failed for the last time.
#
# The general lesson: infrastructure values that application code also reads
# belong in the application's own config, provisioned by a script that imports
# it. Reach for the CLI when the value is infrastructure-only.
#
# HOW TO RUN
#   bash scripts/create_queues.sh
# Idempotent. SQS create-queue succeeds on an existing queue with identical
# attributes, so re-running is safe and is the documented way to recover the
# URLs if you lose them.

set -euo pipefail

export AWS_REGION=ap-southeast-1

# Your AWS account id, looked up rather than hardcoded — so this script works
# in whatever account your credentials point at, not just the one it was
# written in.
ACC=$(aws sts get-caller-identity --query Account --output text)

# ---------------------------------------------------------------------------
# Step 1 — a Python environment
# ---------------------------------------------------------------------------
# bootstrap.py imports only `shared.queues` (boto3) and `shared.config`
# (pydantic-settings), and shared/__init__.py is empty — so those two packages
# are the entire dependency set.
#
# We deliberately do NOT `pip install -r shared/requirements.txt` here.
# That file is the union of what the API and both workers need, including
# psycopg2-binary, which has no prebuilt wheel for Python 3.14 and fails at
# install with a compiler error that has nothing to do with the task at hand.
# Install what the script imports, not what the project contains.
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet 'boto3>=1.34,<2' 'pydantic-settings>=2.3,<3'
./.venv/bin/python -c "import boto3, pydantic_settings; print('boto3', boto3.__version__, 'OK')"

# ---------------------------------------------------------------------------
# Step 2 — .env, and the one field that silently ruins this
# ---------------------------------------------------------------------------
# bootstrap.py reads settings from .env through pydantic-settings. Two fields
# matter before running it.
#
# AWS_REGION IS THE DANGEROUS ONE. shared/queues.py builds its boto3 client at
# IMPORT time:
#
#     client = boto3.client("sqs", region_name=settings.aws_region, ...)
#
# from settings, NOT from your shell's AWS_REGION and not from `aws configure`.
# .env.example ships with us-east-1. Leave it and the queues are created in
# us-east-1 — successfully, with no warning — while everything else you built
# lives in ap-southeast-1. The failure surfaces much later as a worker that
# polls an empty queue forever.
sed -e "s#^S3_BUCKET=.*#S3_BUCKET=edgentrag-v3-${ACC}#" \
    -e 's#^AWS_REGION=.*#AWS_REGION=ap-southeast-1#' \
    .env.example > .env
chmod 600 .env

# Confirm before running anything. Cheap; saves an afternoon.
grep -E '^(S3_BUCKET|AWS_REGION)=' .env

# ---------------------------------------------------------------------------
# Step 3 — run the bootstrap
# ---------------------------------------------------------------------------
# Creates FOUR work queues, each paired with its own dead-letter queue:
#
#   ingest   one message per uploaded file    drained by our ECS ingest-worker
#   chat     one message per question         drained by our ECS chat-worker
#   stt      transcription jobs               drained by the Colab GPU, via the broker
#   embed    embedding jobs                   drained by the Colab GPU, via the broker
#
# Why ingest and chat are separate queues rather than one: sharing would put
# somebody's fifty-file upload in front of everybody else's questions. Bulk
# work and interactive work have different latency requirements, so they get
# different queues and independently autoscaled consumers.
#
# Why stt and embed are separate from both: they are drained by a machine that
# holds no AWS credentials. The GPU never sees these URLs — only the broker
# does (api/routes/broker.py: QUEUES maps a job name to a queue URL, and
# rejects any name not in that map).
./.venv/bin/python -m scripts.bootstrap

# The attributes it set, and why each one:
#
#   VisibilityTimeout 900         when a worker picks up a message it becomes
#                                 invisible for 15 min rather than deleted. Die
#                                 mid-job and it reappears for someone else.
#                                 shared/worker.py extends this with a
#                                 heartbeat for jobs that outlive it.
#   ReceiveMessageWaitTimeSeconds 20   long polling. One 20-second call that
#                                 returns the instant a message arrives, not a
#                                 tight loop of empty requests. Faster to react
#                                 AND dramatically cheaper — SQS bills per
#                                 request.
#   MessageRetentionPeriod 345600 four days. How long an unconsumed message
#                                 survives; a long weekend outage stays
#                                 recoverable.
#   RedrivePolicy maxReceiveCount 5   after five failed deliveries the message
#                                 moves to the DLQ instead of retrying
#                                 forever. THE DLQ IS A DIAGNOSTIC TOOL: when a
#                                 file is stuck, its depth is the first thing
#                                 to check.

# ---------------------------------------------------------------------------
# Step 4 — capture the output IMMEDIATELY
# ---------------------------------------------------------------------------
# BROKER_TOKEN is generated fresh on each run and printed once. It is not
# stored anywhere by the script. Lose it before saving and you must re-run and
# reconcile everything that already holds the old value.
#
# Secrets Manager for the token (it is a credential); SSM Parameter Store for
# the queue URLs (they are not secret — an ARN reveals nothing an attacker
# could use without IAM permission — and String parameters are free).
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name edgentrag-v3/broker-token \
  --description "Bearer token the Colab GPU presents to /broker/*. Grants 'ask for a job' only." \
  --secret-string "<the BROKER_TOKEN printed above>" \
  --tags Key=Project,Value=edgentrag-v3

Q=https://sqs.ap-southeast-1.amazonaws.com/${ACC}/edgentrag-v3
for n in ingest chat stt embed; do
  aws ssm put-parameter --region "$AWS_REGION" \
    --name "/edgentrag-v3/${n}-queue-url" --type String --value "${Q}-${n}" --overwrite
done

# Write the same values back into .env. This makes bootstrap.py idempotent on
# a re-run: it only generates a NEW broker token when settings.broker_token is
# empty, so once .env has one, re-running to recover lost queue URLs will not
# silently rotate the token out from under the Colab services.
#   (sed -i '' is the BSD/macOS spelling. On Linux it is sed -i.)
sed -i '' \
  -e "s#^INGEST_QUEUE_URL=.*#INGEST_QUEUE_URL=${Q}-ingest#" \
  -e "s#^CHAT_QUEUE_URL=.*#CHAT_QUEUE_URL=${Q}-chat#" \
  -e "s#^STT_QUEUE_URL=.*#STT_QUEUE_URL=${Q}-stt#" \
  -e "s#^EMBED_QUEUE_URL=.*#EMBED_QUEUE_URL=${Q}-embed#" \
  -e "s#^BROKER_TOKEN=.*#BROKER_TOKEN=<the BROKER_TOKEN printed above>#" \
  .env

# ---------------------------------------------------------------------------
# Step 5 — verify
# ---------------------------------------------------------------------------
# Expect exactly 8: four work queues and four -dlq.
aws sqs list-queues --region "$AWS_REGION" --queue-name-prefix edgentrag-v3 \
  --query 'length(QueueUrls)' --output text

aws sqs list-queues --region "$AWS_REGION" --queue-name-prefix edgentrag-v3 \
  --query 'QueueUrls[]' --output text | tr '\t' '\n' | sed 's#.*/##' | sort

# Confirm the redrive policy actually attached. A queue with no RedrivePolicy
# retries a poison message forever — the specific failure this prevents.
aws sqs get-queue-attributes --region "$AWS_REGION" --queue-url "${Q}-ingest" \
  --attribute-names VisibilityTimeout ReceiveMessageWaitTimeSeconds MessageRetentionPeriod RedrivePolicy \
  --query Attributes --output json

# Everything Phase A stored, in one view.
aws ssm get-parameters-by-path --region "$AWS_REGION" --path /edgentrag-v3 \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table

# ---------------------------------------------------------------------------
# SECURITY NOTE — .env now contains a live credential
# ---------------------------------------------------------------------------
# BROKER_TOKEN is in .env in plaintext. This directory is not a git repo
# today; the moment it becomes one, .env must be in .gitignore BEFORE the
# first commit. A token committed once is in history forever, and the only
# real fix is rotation.
#
# Rotating it, if that happens: generate a new value, update the Secrets
# Manager secret, redeploy the EC2 instances so the API picks it up, and edit
# services/.env on Colab. Two places, because the broker token is a shared
# secret between exactly those two sides.
#
# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
#   for q in ingest chat stt embed; do
#     aws sqs delete-queue --region $AWS_REGION --queue-url ${Q}-$q
#     aws sqs delete-queue --region $AWS_REGION --queue-url ${Q}-$q-dlq
#   done
#   aws secretsmanager delete-secret --region $AWS_REGION --secret-id edgentrag-v3/broker-token --force-delete-without-recovery
#
# SQS refuses to recreate a queue with the same name for 60 SECONDS after
# deletion. If a teardown-and-rebuild demo fails with QueueDeletedRecently,
# that is why — wait a minute.
#
# ---------------------------------------------------------------------------
# PHASE A IS NOW COMPLETE. Everything stateful exists; nothing runs yet.
#
# NEXT: Phase B — Cognito. Doing it now rather than at DEPLOY.md's section 8
# avoids rebuilding the frontend image: the backend reads COGNITO_* at
# container start, but the frontend bakes VITE_COGNITO_* in at `vite build`.
# See DEPLOY_PLAN.md, "Deviation 1".
