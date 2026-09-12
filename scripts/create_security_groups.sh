#!/usr/bin/env bash
#
# Task 1 of the EdgentRAG v3 deployment — the five security groups.
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# WHY THIS IS TASK ONE
# --------------------
# Security groups are a firewall that names *other security groups* instead of
# IP addresses. "Postgres accepts connections from whatever is in the group
# edgentrag-v3-ec2" stays true when an instance is replaced, when the Auto
# Scaling Group doubles, when a Fargate task gets a new private IP. An IP-based
# rule would be wrong within minutes of the first scaling event.
#
# That design creates a chicken-and-egg problem: the RDS group needs to allow
# the EC2 and ECS groups, which do not exist yet. DEPLOY.md section 2
# acknowledges this and suggests placeholders. The cleaner fix is here —
# create all five EMPTY first, then wire the rules once every id exists.
# Security groups cost nothing, so an empty one is a free forward declaration.
#
#   Step 1  create all five, empty
#   Step 2  tag them
#   Step 3  add the inbound rules, now that every id is known
#
# HOW TO RUN
#   bash scripts/create_security_groups.sh
# It is NOT idempotent — a second run fails with InvalidGroup.Duplicate.
# That is deliberate for teaching: the error names exactly what already exists.
# See the teardown block at the bottom to start over.

set -euo pipefail

# ---------------------------------------------------------------------------
# Step 0 — confirm which account and region we are about to change
# ---------------------------------------------------------------------------

# Who am I? Prints account id and IAM user. Always run this before creating
# anything; it is the cheapest possible guard against building in the wrong
# account. (Note: DEPLOY.md was written for account <upstream-account-id> in us-east-1 —
# a different account from this one. Every ARN in that runbook needs swapping.)
aws sts get-caller-identity

export AWS_REGION=ap-southeast-1

# Your AWS account id, looked up rather than hardcoded — so this script works
# in whatever account your credentials point at, not just the one it was
# written in.
ACC=$(aws sts get-caller-identity --query Account --output text)

# Find the default VPC. Security groups belong to exactly one VPC and can only
# reference other groups in that same VPC, so everything below must agree.
# --filters narrows server-side; --query reshapes the JSON client-side.
aws ec2 describe-vpcs --region "$AWS_REGION" \
  --filters "Name=isDefault,Values=true" \
  --query 'Vpcs[0].VpcId' --output text
VPC=vpc-07a411d32015cc621

# Confirm no edgentrag-v3 groups exist yet. Empty output means a clean start.
# Group names are unique per VPC, so a leftover from a previous attempt would
# make Step 1 fail.
aws ec2 describe-security-groups --region "$AWS_REGION" \
  --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=edgentrag-v3-*" \
  --query 'SecurityGroups[].GroupName' --output text

# ---------------------------------------------------------------------------
# Step 1 — create all five groups, empty
# ---------------------------------------------------------------------------
# A new security group starts with NO inbound rules (deny all in) and ONE
# outbound rule (allow all out). So at this point all five are already in
# their safe default state: nothing can reach them, they can reach out.
#
# --query GroupId --output text strips the JSON wrapper so the id can be
# captured straight into a shell variable. --description is mandatory on AWS's
# side; we use it as documentation that survives outside this file.

# The public front door. The only group that will ever accept traffic from the
# internet. Sits in public subnets.
ALB=$(aws ec2 create-security-group --region "$AWS_REGION" --vpc-id "$VPC" \
  --group-name edgentrag-v3-alb \
  --description "Public entry point: the Application Load Balancer" \
  --query GroupId --output text)

# The EC2 Auto Scaling Group instances. Each runs two containers from
# docker-compose.ec2.yml: nginx (web) on 8080 and FastAPI (api) on 8000.
# Only 8080 is ever exposed to the host — see the "no ports" comment on the
# api service in that compose file.
EC2=$(aws ec2 create-security-group --region "$AWS_REGION" --vpc-id "$VPC" \
  --group-name edgentrag-v3-ec2 \
  --description "EC2 Auto Scaling Group instances running web + api" \
  --query GroupId --output text)

# The two Fargate worker services (ingest-worker, chat-worker). These never
# listen on a port at all — they long-poll SQS outbound. This group will
# deliberately keep ZERO inbound rules forever.
ECS=$(aws ec2 create-security-group --region "$AWS_REGION" --vpc-id "$VPC" \
  --group-name edgentrag-v3-ecs \
  --description "ECS Fargate worker tasks: ingest-worker and chat-worker" \
  --query GroupId --output text)

# RDS PostgreSQL. Holds both the relational data and — after migration
# 0002_pgvector — the vectors themselves, in chunks.embedding.
RDS=$(aws ec2 create-security-group --region "$AWS_REGION" --vpc-id "$VPC" \
  --group-name edgentrag-v3-rds \
  --description "RDS PostgreSQL with pgvector" \
  --query GroupId --output text)

# ElastiCache Redis. Four jobs: SSE pub/sub, the conversation window, the
# Colab service addresses, and SSE tickets. See shared/events.py.
REDIS=$(aws ec2 create-security-group --region "$AWS_REGION" --vpc-id "$VPC" \
  --group-name edgentrag-v3-redis \
  --description "ElastiCache Redis: conversation window and SSE pub/sub" \
  --query GroupId --output text)

# The ids this run produced. Yours will differ — ids are generated by AWS.
#   ALB    sg-038544d1abfc26b09
#   EC2    sg-073c71a2cc4551d9e
#   ECS    sg-029616add5e208bbc
#   RDS    sg-0ff59fddbc464726a
#   REDIS  sg-0d759d09c50fcad20
printf 'ALB=%s\nEC2=%s\nECS=%s\nRDS=%s\nREDIS=%s\n' "$ALB" "$EC2" "$ECS" "$RDS" "$REDIS"

# ---------------------------------------------------------------------------
# Step 2 — tag all five at once
# ---------------------------------------------------------------------------
# create-tags takes multiple --resources in one call. The tag is what makes
# "show me everything belonging to this project" and "delete everything
# belonging to this project" one command each, instead of five.
aws ec2 create-tags --region "$AWS_REGION" \
  --resources "$ALB" "$EC2" "$ECS" "$RDS" "$REDIS" \
  --tags Key=Project,Value=edgentrag-v3

# ---------------------------------------------------------------------------
# Step 3 — the inbound rules
# ---------------------------------------------------------------------------
# Read these as a chain. Each tier accepts traffic from exactly the tier in
# front of it and nothing else:
#
#     internet --443--> ALB --8080--> EC2 --5432--> RDS
#                                      |   --6379--> Redis
#                                     ECS --5432/6379 (same two backends)
#
# Two syntaxes exist for this command. The short one (--protocol/--port/--cidr)
# cannot attach a description or list several sources in one call, so every
# rule below uses --ip-permissions instead. Descriptions are worth the extra
# typing: they show in the console next to the rule, six months from now.

# RULE 1 — ALB accepts HTTPS from anywhere.
# IpRanges means a CIDR block; 0.0.0.0/0 is "the whole internet". This is the
# ONLY rule in the whole system that is open to the world.
# After CloudFront exists (DEPLOY.md section 10), replace this with CloudFront's
# managed prefix list so nobody can bypass the edge — and therefore WAF — by
# hitting the ALB's DNS name directly.
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$ALB" \
  --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description="HTTPS from the internet (tighten to CloudFront prefix list after DEPLOY.md 10)"}]'

# RULE 2 — EC2 accepts 8080 from the ALB group ONLY.
# UserIdGroupPairs (not IpRanges) is the important part: the source is a
# GROUP, not an address. Any instance the Auto Scaling Group launches is
# covered the instant it boots, with no rule to update.
# There is deliberately no SSH rule here. Use SSM Session Manager if you need
# shell access; opening 22 to the world is how course projects get mined.
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$EC2" \
  --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$ALB,Description=\"nginx, from the ALB only\"}]"

# RULE 3 — Postgres accepts 5432 from the API tier AND the worker tier.
# Both need it: the API reads and writes rows, and the workers do the bulk
# UPDATE of chunks.embedding (shared/vectorstore.py) and the similarity search.
# Two pairs in ONE --ip-permissions argument = two rules from one API call.
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$RDS" \
  --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$EC2,Description=\"api on EC2\"},{GroupId=$ECS,Description=\"ingest and chat workers on Fargate\"}]"

# RULE 4 — Redis accepts 6379 from the same two tiers, for the same reason.
# The pub/sub channel has a publisher (a worker) and a subscriber (whichever
# API task holds the browser's SSE connection) on opposite sides of this rule.
# Cut either side and progress events stop reaching the browser.
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$REDIS" \
  --ip-permissions "IpProtocol=tcp,FromPort=6379,ToPort=6379,UserIdGroupPairs=[{GroupId=$EC2,Description=\"api on EC2\"},{GroupId=$ECS,Description=\"ingest and chat workers on Fargate\"}]"

# RULE 5 — there isn't one. edgentrag-v3-ecs keeps ZERO inbound rules.
# This is the point worth pausing on with students. The workers are a major
# part of the system and nothing can initiate a connection to them, ever.
# They pull work from SQS, and results travel back through Redis and Postgres.
# A queue-based architecture is what makes that possible — an HTTP-based one
# would have forced an open port here.

# ---------------------------------------------------------------------------
# Step 4 — verify
# ---------------------------------------------------------------------------
# Expected: five rows. ecs shows 0 inbound; the other four show 1 each.
# "1 inbound rule" on rds/redis means one PERMISSION entry that contains two
# source groups — count sources, not entries, when checking your work.
aws ec2 describe-security-groups --region "$AWS_REGION" \
  --filters "Name=tag:Project,Values=edgentrag-v3" \
  --query 'SecurityGroups[].{Name:GroupName,Id:GroupId,InboundRules:length(IpPermissions)}' \
  --output table

# Confirm exactly two groups can reach Postgres, and that they are EC2 and ECS.
aws ec2 describe-security-groups --region "$AWS_REGION" --group-ids "$RDS" \
  --query 'SecurityGroups[0].IpPermissions[].{Port:FromPort,FromSG:UserIdGroupPairs[].GroupId}' \
  --output json

# ---------------------------------------------------------------------------
# Teardown — to start over
# ---------------------------------------------------------------------------
# Order matters. A group cannot be deleted while another group's rule still
# references it, so the referenced groups (alb, ec2, ecs) must go LAST.
# Revoking the rules first removes every reference, after which order is free.
#
#   aws ec2 revoke-security-group-ingress --region $AWS_REGION --group-id $RDS   --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$EC2},{GroupId=$ECS}]"
#   aws ec2 revoke-security-group-ingress --region $AWS_REGION --group-id $REDIS --ip-permissions "IpProtocol=tcp,FromPort=6379,ToPort=6379,UserIdGroupPairs=[{GroupId=$EC2},{GroupId=$ECS}]"
#   aws ec2 revoke-security-group-ingress --region $AWS_REGION --group-id $EC2   --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$ALB}]"
#   for sg in $RDS $REDIS $EC2 $ECS $ALB; do aws ec2 delete-security-group --region $AWS_REGION --group-id $sg; done
#
# ---------------------------------------------------------------------------
# NEXT: task 2 — RDS PostgreSQL 15+, attached to $RDS above (DEPLOY.md 2),
# and ElastiCache Redis attached to $REDIS (DEPLOY.md 3, cluster mode DISABLED).
# Start both before the SQS bootstrap; they take several minutes to provision.
