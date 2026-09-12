#!/usr/bin/env bash
#
# Task 2 of the EdgentRAG v3 deployment — RDS PostgreSQL and ElastiCache Redis.
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# Prerequisite: scripts/create_security_groups.sh (task 1). This script attaches
# the two groups it created:
#   edgentrag-v3-rds    sg-0ff59fddbc464726a   -> the database
#   edgentrag-v3-redis  sg-0d759d09c50fcad20   -> the cache
#
# WHY THESE TWO TOGETHER
# ----------------------
# They are the only stateful things in v3, they are independent of each other,
# and each takes five to ten minutes to provision. Start both back to back and
# do the SQS bootstrap while they build — that is the whole reason task 2 is
# one script and not two.
#
# WHAT THEY ARE FOR
#   RDS    relational rows AND the vectors. chunks.embedding is a
#          pgvector column, so v3 has no separate vector database at all.
#   Redis  four jobs, none of them a cache in the usual sense:
#            1. pub/sub that carries progress from a worker to whichever API
#               task holds the browser's SSE connection   <- the load-bearing one
#            2. the conversation window (last N turns, 24h TTL)
#            3. where the three Colab service addresses currently are
#            4. short-lived SSE tickets
#          See shared/events.py, shared/services.py, routes/tickets.py.
#
# HOW TO RUN
#   bash scripts/create_database_and_cache.sh
# Not idempotent — a second run fails with DBInstanceAlreadyExists.

set -euo pipefail

export AWS_REGION=ap-southeast-1

# Your AWS account id, looked up rather than hardcoded — so this script works
# in whatever account your credentials point at, not just the one it was
# written in.
ACC=$(aws sts get-caller-identity --query Account --output text)
VPC=vpc-07a411d32015cc621
SG_RDS=sg-0ff59fddbc464726a
SG_REDIS=sg-0d759d09c50fcad20

# ---------------------------------------------------------------------------
# Step 1 — check what the region actually offers, before choosing
# ---------------------------------------------------------------------------
# Do not copy a version number out of a tutorial. Engine versions are retired
# on a schedule, and the set available differs by region. Ask.

# pgvector needs PostgreSQL 15 or newer. This lists what is currently
# orderable; we picked 16.15 — comfortably past the 15 floor, and not the
# newest major, which is the conservative choice for a dependency
# (pgvector) that trails the engine.
aws rds describe-db-engine-versions --region "$AWS_REGION" --engine postgres \
  --query 'DBEngineVersions[?starts_with(EngineVersion,`16.`)].EngineVersion' --output text

# Confirm the cheap instance class is actually orderable here. A non-empty
# count means yes. Instance-class availability varies by region and engine.
aws rds describe-orderable-db-instance-options --region "$AWS_REGION" --engine postgres \
  --db-instance-class db.t4g.micro --query 'length(OrderableDBInstanceOptions)' --output text

# ---------------------------------------------------------------------------
# Step 2 — subnet groups
# ---------------------------------------------------------------------------
# Neither RDS nor ElastiCache takes a list of subnets directly. Both want a
# named SUBNET GROUP: "here are the subnets, in these availability zones,
# where you may place this thing." Multiple AZs in the group is what makes a
# future Multi-AZ failover possible without rebuilding anything.
#
# The default VPC gives us three subnets in three AZs. Note they are all
# PUBLIC (MapPublicIpOnLaunch=true). DEPLOY.md assumes private subnets, which
# is the better end state. What makes this acceptable here is that RDS is
# created with --no-publicly-accessible (no public IP, no public DNS) and its
# security group only admits the EC2 and ECS groups. The subnet is not the
# thing keeping the database private — the security group is.
aws ec2 describe-subnets --region "$AWS_REGION" --filters "Name=vpc-id,Values=$VPC" \
  --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone,PublicIP:MapPublicIpOnLaunch}' --output table

SUBNETS="subnet-0a466fb1f2d3a1e8f subnet-06ecb1c97f87a5545 subnet-0ac3b9285a4d3e530"

aws rds create-db-subnet-group --region "$AWS_REGION" \
  --db-subnet-group-name edgentrag-v3-db \
  --db-subnet-group-description "Subnets for the edgentrag-v3 RDS instance" \
  --subnet-ids $SUBNETS

# Same idea, different service, annoyingly different command name and flags.
aws elasticache create-cache-subnet-group --region "$AWS_REGION" \
  --cache-subnet-group-name edgentrag-v3-cache \
  --cache-subnet-group-description "Subnets for the edgentrag-v3 ElastiCache Redis cluster" \
  --subnet-ids $SUBNETS

# ---------------------------------------------------------------------------
# Step 3 — the master password
# ---------------------------------------------------------------------------
# Generated locally, written straight to a file with 600 permissions, and
# never echoed to the terminal. The point for students: a password that is
# printed is a password that is now in your shell history, your scrollback,
# and quite possibly your screen recording.
#
# Alphanumeric ONLY, deliberately. RDS forbids / " @ and space, and the app
# consumes this as part of a postgresql:// URL — so a character needing
# percent-encoding would produce a URL that parses wrong in a way that is
# genuinely painful to debug. 32 alphanumeric characters is ~190 bits; the
# restricted alphabet costs nothing that matters.
#
# We do NOT use --manage-master-user-password (RDS-managed rotation). It
# rotates the password on a schedule, which would silently invalidate the
# static DATABASE_URL this app reads at container start. Rotation and a
# hardcoded connection URL are incompatible; pick one. For this app: a
# self-managed password.
LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32 > ./db-pass.txt
chmod 600 ./db-pass.txt

# ---------------------------------------------------------------------------
# Step 4 — create the database
# ---------------------------------------------------------------------------
# Returns immediately with Status: creating. Provisioning takes 5-10 minutes.
#
# Flags worth understanding rather than copying:
#   --db-name edgentrag        creates an initial database INSIDE the instance.
#                              Omit it and you get a running server with no
#                              database, and a confusing connection failure.
#   --no-publicly-accessible   no public IP, no public DNS name. With the
#                              security group, this is the actual privacy
#                              boundary.
#   --storage-type gp3         and no --max-allocated-storage, so storage
#                              autoscaling stays OFF. A runaway table should
#                              produce an error, not a surprise bill.
#   --backup-retention-period 1  one day. 0 disables automated backups entirely
#                              AND disables point-in-time recovery. 1 is the
#                              cheapest setting that is not reckless.
#   --no-multi-az              one AZ. Halves the cost; accepts that a zone
#                              failure means downtime. Correct for a course
#                              project, wrong for production.
#   --no-auto-minor-version-upgrade  no unattended restarts during a demo.
aws rds create-db-instance --region "$AWS_REGION" \
  --db-instance-identifier edgentrag-v3 \
  --db-instance-class db.t4g.micro \
  --engine postgres \
  --engine-version 16.15 \
  --master-username edgentrag \
  --master-user-password "$(cat ./db-pass.txt)" \
  --db-name edgentrag \
  --allocated-storage 20 \
  --storage-type gp3 \
  --db-subnet-group-name edgentrag-v3-db \
  --vpc-security-group-ids "$SG_RDS" \
  --no-publicly-accessible \
  --backup-retention-period 1 \
  --no-multi-az \
  --no-auto-minor-version-upgrade \
  --copy-tags-to-snapshot \
  --tags Key=Project,Value=edgentrag-v3

# Nothing to do here for pgvector. `CREATE EXTENSION vector` is Alembic
# migration 0002_pgvector, run later as a one-off ECS task. RDS grants that
# extension to the master user by default on PG 15+.

# ---------------------------------------------------------------------------
# Step 5 — create the cache (immediately, do not wait for RDS)
# ---------------------------------------------------------------------------
# create-replication-group, NOT create-cache-cluster. Two reasons:
#
#   1. In-transit encryption is only available on a replication group. That
#      is what makes the connection string rediss:// instead of redis://.
#   2. It is the shape that can later gain a replica without a rebuild.
#
# CLUSTER MODE IS DISABLED, and that is the single most important line here.
# Omitting --num-node-groups leaves cluster mode off. Turning it on shards the
# keyspace, and Redis pub/sub does not reliably fan out across shards — so the
# SSE stream would silently stop reaching some browsers, with no error
# anywhere. Confirm ClusterEnabled:false in the output below.
#
# --num-cache-clusters 1 means one node, no replica: fine for a course
# project, and the reason there is no failover story here.
aws elasticache create-replication-group --region "$AWS_REGION" \
  --replication-group-id edgentrag-v3 \
  --replication-group-description "EdgentRAG v3: SSE pub/sub, conversation window, service addresses, SSE tickets" \
  --engine redis \
  --engine-version 7.1 \
  --cache-node-type cache.t4g.micro \
  --num-cache-clusters 1 \
  --cache-subnet-group-name edgentrag-v3-cache \
  --security-group-ids "$SG_REDIS" \
  --transit-encryption-enabled \
  --at-rest-encryption-enabled \
  --no-auto-minor-version-upgrade \
  --tags Key=Project,Value=edgentrag-v3

# ---------------------------------------------------------------------------
# Step 6 — block until both are ready
# ---------------------------------------------------------------------------
# `aws ... wait` polls for you instead of you re-running describe by hand.
# Each returns exit code 0 when ready, non-zero if it gives up. Run them
# sequentially — they are independent, but the second is usually ready by the
# time the first returns anyway.
aws rds wait db-instance-available --region "$AWS_REGION" --db-instance-identifier edgentrag-v3
aws elasticache wait replication-group-available --region "$AWS_REGION" --replication-group-id edgentrag-v3

# ---------------------------------------------------------------------------
# Step 7 — collect the two endpoints
# ---------------------------------------------------------------------------
# These only exist once provisioning finishes, which is why this is a separate
# step and not part of the create call's output.
RDS_HOST=$(aws rds describe-db-instances --region "$AWS_REGION" \
  --db-instance-identifier edgentrag-v3 \
  --query 'DBInstances[0].Endpoint.Address' --output text)

# NodeGroups[0].PrimaryEndpoint is the writer. Always connect to the primary,
# never to a node address directly — the node can be replaced, the primary
# endpoint follows it.
REDIS_HOST=$(aws elasticache describe-replication-groups --region "$AWS_REGION" \
  --replication-group-id edgentrag-v3 \
  --query 'ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint.Address' --output text)

echo "RDS_HOST=$RDS_HOST"
echo "REDIS_HOST=$REDIS_HOST"

# ---------------------------------------------------------------------------
# Step 8 — build the two connection URLs
# ---------------------------------------------------------------------------
# postgresql:// and NOT postgres://. RDS consoles and many tutorials emit the
# latter; shared/db.py::_driver rewrites it defensively, but v2's own tutorial
# flagged this scheme as "a URL scheme that crashed everything". Write it
# correctly and the defensive code never has to run.
DATABASE_URL="postgresql://edgentrag:$(cat ./db-pass.txt)@${RDS_HOST}:5432/edgentrag"

# rediss:// with two s's — that second s is TLS, and it is required because we
# created the group with --transit-encryption-enabled. Plain redis:// against
# this endpoint hangs rather than failing cleanly, which is a memorable
# afternoon. redis.Redis.from_url understands rediss:// natively, so this is
# configuration only — no code change anywhere.
REDIS_URL="rediss://${REDIS_HOST}:6379/0"

# ---------------------------------------------------------------------------
# Step 9 — store both in Secrets Manager
# ---------------------------------------------------------------------------
# This is DEPLOY.md section 5's job, done now because the values exist now and
# they contain a password. They go to Secrets Manager rather than SSM
# Parameter Store because ECS task definitions can inject a secret into an
# environment variable without it ever appearing in the task definition JSON,
# which IS visible to anyone with console read access.
#
# The names match DEPLOY.md exactly, so the ECS and EC2 wiring later can be
# copied without edits.
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name edgentrag-v3/database-url \
  --description "Full postgresql:// URL for the edgentrag-v3 RDS instance" \
  --secret-string "$DATABASE_URL" \
  --tags Key=Project,Value=edgentrag-v3

aws secretsmanager create-secret --region "$AWS_REGION" \
  --name edgentrag-v3/redis-url \
  --description "Full rediss:// URL for the edgentrag-v3 ElastiCache cluster" \
  --secret-string "$REDIS_URL" \
  --tags Key=Project,Value=edgentrag-v3

# The password file has now served its purpose. The authoritative copy lives
# in Secrets Manager; a second copy on a laptop is pure liability.
rm -f ./db-pass.txt

# ---------------------------------------------------------------------------
# Step 10 — verify
# ---------------------------------------------------------------------------
# Expect: available, 16.15, PubliclyAccessible false, MultiAZ false.
aws rds describe-db-instances --region "$AWS_REGION" --db-instance-identifier edgentrag-v3 \
  --query 'DBInstances[0].{Status:DBInstanceStatus,Engine:EngineVersion,Public:PubliclyAccessible,MultiAZ:MultiAZ,Endpoint:Endpoint.Address}' \
  --output table

# Expect: available, ClusterEnabled false, TransitEncryption enabled.
# If ClusterEnabled is true, delete and recreate — it cannot be changed in
# place, and pub/sub will not work correctly.
aws elasticache describe-replication-groups --region "$AWS_REGION" --replication-group-id edgentrag-v3 \
  --query 'ReplicationGroups[0].{Status:Status,ClusterEnabled:ClusterEnabled,TLS:TransitEncryptionEnabled,Endpoint:NodeGroups[0].PrimaryEndpoint.Address}' \
  --output table

# Confirm both secrets exist. This prints names and ARNs only, never values —
# reading a value back is `get-secret-value`, and you rarely need to.
aws secretsmanager list-secrets --region "$AWS_REGION" \
  --filters Key=name,Values=edgentrag-v3 \
  --query 'SecretList[].{Name:Name,ARN:ARN}' --output table

# NOTE ON CONNECTIVITY: you cannot psql to this database from your laptop, and
# that is correct. It has no public endpoint and its security group only
# admits the EC2 and ECS groups, neither of which exists yet. The first real
# proof the database works is the Alembic migration task (DEPLOY.md 6.4),
# which runs from inside the VPC. Resist the urge to "just open it up to my IP
# to test" — that rule tends to survive into production.

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
#   aws rds delete-db-instance --region $AWS_REGION --db-instance-identifier edgentrag-v3 --skip-final-snapshot --delete-automated-backups
#   aws elasticache delete-replication-group --region $AWS_REGION --replication-group-id edgentrag-v3
#   aws secretsmanager delete-secret --region $AWS_REGION --secret-id edgentrag-v3/database-url --force-delete-without-recovery
#   aws secretsmanager delete-secret --region $AWS_REGION --secret-id edgentrag-v3/redis-url  --force-delete-without-recovery
#   aws rds delete-db-subnet-group --region $AWS_REGION --db-subnet-group-name edgentrag-v3-db
#   aws elasticache delete-cache-subnet-group --region $AWS_REGION --cache-subnet-group-name edgentrag-v3-cache
#
# Without --force-delete-without-recovery a deleted secret sits in a 30-day
# recovery window AND keeps its name reserved — so recreating it fails with
# "already scheduled for deletion", which is a confusing error the first time.
#
# ---------------------------------------------------------------------------
# NEXT: task 3 — an S3 bucket, then `python -m scripts.bootstrap` to create the
# eight SQS queues (4 work + 4 dead-letter), then the remaining SSM parameters.
