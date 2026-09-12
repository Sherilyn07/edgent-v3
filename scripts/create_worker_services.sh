#!/usr/bin/env bash
#
# Task 7 of the EdgentRAG v3 deployment — the two ECS Fargate worker services.
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# THIS IS WHERE THE SYSTEM STARTS RUNNING CONTINUOUSLY (and costing money by
# the hour). Two long-lived services, each polling its own queue.
#
# WHY TWO SERVICES AND NOT ONE
# ----------------------------
# Version 2 built ONE worker image and ran it twice with different `command:`
# values. v3 splits them, and the sizes show why it was worth doing:
#
#     ingest-worker   3474 MB   Docling, torch, libgl — converts documents
#     chat-worker      116 MB   no Docling, no S3 at all
#
# 30x. The chat worker is on the interactive path — somebody is waiting for an
# answer — so its cold start matters and its autoscaling should be responsive.
# Making it pull 3.4 GB to do a SQL query and an HTTP call would be absurd.
#
# They also have genuinely different failure profiles: ingest jobs run for
# minutes and must survive being retried; chat jobs run for seconds and should
# come back to the queue fast if they stall. That is why their stopTimeout and
# heartbeat differ.
#
# HOW TO RUN
#   bash scripts/create_worker_services.sh

set -euo pipefail

export AWS_REGION=ap-southeast-1
ACC=$(aws sts get-caller-identity --query Account --output text)
REG=$ACC.dkr.ecr.$AWS_REGION.amazonaws.com

# ---------------------------------------------------------------------------
# A ZSH TRAP THAT COST US A DEBUGGING CYCLE — read this before anything else
# ---------------------------------------------------------------------------
# Task 6 built the queue ARNs like this:
#
#     Q=arn:aws:sqs:$AWS_REGION:$ACC:edgentrag-v3      # WRONG in zsh
#
# and the policy that reached IAM contained:
#
#     arn:aws:sqs:ap-southeast-1:dgentrag-v3-chat
#      ...............................^ account id gone, and the "e" eaten
#
# In zsh, `:e` following a parameter expansion is a HISTORY MODIFIER meaning
# "the file extension of this value". `$ACC:e` therefore expanded to the
# extension of ${ACC} — there is no dot, so: the empty string — and the
# ":e" was consumed as syntax rather than text. bash has no such modifier, so
# the identical line is fine there. A script that works for the instructor and
# breaks for the student is usually something in this family.
#
# The fix is braces, which end the parameter name explicitly:
Q="arn:aws:sqs:${AWS_REGION}:${ACC}:edgentrag-v3"
QURL="https://sqs.${AWS_REGION}.amazonaws.com/${ACC}/edgentrag-v3"
echo "$Q"    # sanity-check EVERY constructed ARN before it goes into a policy

# The symptom, if you skip that check, is the container crash-looping with:
#
#   botocore.exceptions.ClientError: An error occurred (AccessDenied) when
#   calling the ReceiveMessage operation: ... is not authorized to perform:
#   sqs:receivemessage on resource: arn:aws:sqs:...:edgentrag-v3-chat
#
# Note it names the resource it WANTED, not the malformed one in the policy —
# so the error looks like a missing permission rather than a typo'd ARN.
# Always diff what the policy actually says against what the error asked for:
#
#   aws iam get-role-policy --role-name edgentrag-v3-chat-task-role \
#     --policy-name chat-worker --query 'PolicyDocument.Statement[].Resource'

# ---------------------------------------------------------------------------
# Step 1 — task definitions
# ---------------------------------------------------------------------------
DB_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/database-url --query ARN --output text)
RD_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/redis-url    --query ARN --output text)

# INGEST. Sized for document conversion.
#
#   cpu/memory 1024/2048   Docling on a scanned PDF is the heavy case. Raise to
#                          2048/4096 if OCR proves slow.
#   ephemeralStorage 30    the default 20 GiB has to hold the Docling model
#                          cache (HF_HOME) plus a downloaded video plus its
#                          extracted text. Cheap insurance.
#   stopTimeout 120        ECS's maximum. On SIGTERM, shared/worker.py stops
#                          pulling new work and lets the in-flight job finish,
#                          so a rolling deploy never duplicates work. A
#                          document conversion needs the full two minutes.
#   environment            the three queues it touches: consumes ingest, sends
#                          to stt and embed. No CHAT_QUEUE_URL — it never
#                          touches that queue.
#
# NOT here: BROKER_TOKEN. DEPLOY.md section 6.5 lists it; grep proves it is
# unused — settings.broker_token is read only by backend/api/routes/broker.py
# and by services/*/config.py on Colab.
cat > /tmp/ingest-taskdef.json <<EOF
{
  "family": "edgentrag-v3-ingest",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "1024",
  "memory": "2048",
  "ephemeralStorage": {"sizeInGiB": 30},
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::${ACC}:role/edgentrag-v3-execution-role",
  "taskRoleArn": "arn:aws:iam::${ACC}:role/edgentrag-v3-ingest-task-role",
  "containerDefinitions": [{
    "name": "ingest-worker",
    "image": "${REG}/edgentrag-v3/ingest-worker:latest",
    "essential": true,
    "stopTimeout": 120,
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "${DB_ARN}"},
      {"name": "REDIS_URL", "valueFrom": "${RD_ARN}"}
    ],
    "environment": [
      {"name": "ENV", "value": "aws"},
      {"name": "AWS_REGION", "value": "${AWS_REGION}"},
      {"name": "S3_BUCKET", "value": "edgentrag-v3-${ACC}"},
      {"name": "INGEST_QUEUE_URL", "value": "${QURL}-ingest"},
      {"name": "STT_QUEUE_URL",    "value": "${QURL}-stt"},
      {"name": "EMBED_QUEUE_URL",  "value": "${QURL}-embed"}
    ],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/edgentrag-v3-ingest",
      "awslogs-region": "${AWS_REGION}",
      "awslogs-stream-prefix": "ingest"}}
  }]
}
EOF

# CHAT. Half the CPU, half the memory, no ephemeral storage override, and
# notice how SHORT the environment block is.
#
#   no S3_BUCKET       it never touches storage. In v2 this worker called the
#                      embedding service's /retrieve then fetched chunk text
#                      separately; in v3 retrieval is one SQL query
#                      (shared/vectorstore.py::search).
#   no *_SERVICE_URL   the Colab addresses live in Redis (shared/services.py),
#                      written by the app's connect screen. A tunnel hostname
#                      changes on every restart, so baking it into a task
#                      definition would mean a redeploy every time.
#   stopTimeout 60     a question takes seconds; no need for two minutes.
cat > /tmp/chat-taskdef.json <<EOF
{
  "family": "edgentrag-v3-chat",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::${ACC}:role/edgentrag-v3-execution-role",
  "taskRoleArn": "arn:aws:iam::${ACC}:role/edgentrag-v3-chat-task-role",
  "containerDefinitions": [{
    "name": "chat-worker",
    "image": "${REG}/edgentrag-v3/chat-worker:latest",
    "essential": true,
    "stopTimeout": 60,
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "${DB_ARN}"},
      {"name": "REDIS_URL", "valueFrom": "${RD_ARN}"}
    ],
    "environment": [
      {"name": "ENV", "value": "aws"},
      {"name": "AWS_REGION", "value": "${AWS_REGION}"},
      {"name": "CHAT_QUEUE_URL", "value": "${QURL}-chat"}
    ],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/edgentrag-v3-chat",
      "awslogs-region": "${AWS_REGION}",
      "awslogs-stream-prefix": "chat"}}
  }]
}
EOF

aws ecs register-task-definition --region "$AWS_REGION" --cli-input-json file:///tmp/ingest-taskdef.json
aws ecs register-task-definition --region "$AWS_REGION" --cli-input-json file:///tmp/chat-taskdef.json

# ---------------------------------------------------------------------------
# Step 2 — the services
# ---------------------------------------------------------------------------
# No load balancer, no target group, no port mapping. These services accept no
# inbound connections at all — their security group (edgentrag-v3-ecs) has zero
# inbound rules, which is only possible because work arrives by QUEUE rather
# than by HTTP request. Worth pausing on: a major part of this system is
# entirely unreachable from the network.
#
# assignPublicIp=ENABLED because our subnets have no NAT gateway and both
# workers need outbound internet — to pull from ECR, and for chat-worker to
# reach the Colab tunnels. Inbound is still closed by the security group.
#
# --enable-execute-command allows `aws ecs execute-command` to open a shell in
# a running task. Invaluable for teaching ("what does this container actually
# see?") without opening SSH anywhere.
NET="awsvpcConfiguration={subnets=[subnet-0a466fb1f2d3a1e8f,subnet-06ecb1c97f87a5545,subnet-0ac3b9285a4d3e530],securityGroups=[sg-029616add5e208bbc],assignPublicIp=ENABLED}"

for s in ingest chat; do
  aws ecs create-service --region "$AWS_REGION" \
    --cluster edgentrag-v3 \
    --service-name "edgentrag-v3-$s" \
    --task-definition "edgentrag-v3-$s" \
    --desired-count 1 \
    --launch-type FARGATE \
    --network-configuration "$NET" \
    --enable-execute-command \
    --tags key=Project,value=edgentrag-v3
done

# ---------------------------------------------------------------------------
# Step 3 — autoscaling on BACKLOG PER TASK, not raw queue depth
# ---------------------------------------------------------------------------
# This distinction is the whole lesson of this step.
#
# Scaling on ApproximateNumberOfMessagesVisible alone is the obvious approach
# and it is wrong: "50 messages" means something completely different with one
# worker than with ten. The metric you actually want is messages DIVIDED BY
# running tasks — how far behind is each worker — because that is the quantity
# that stays meaningful as the fleet changes size. It is also what makes
# target tracking converge instead of oscillate.
#
# CloudWatch has no such metric, so we build it with METRIC MATH: two source
# metrics, one expression, only the expression returning data.
#
# RunningTaskCount comes from ECS/ContainerInsights, which must be enabled:
aws ecs update-cluster-settings --region "$AWS_REGION" --cluster edgentrag-v3 \
  --settings name=containerInsights,value=enabled

# min-capacity 1, never 0: a single upload should not wait on a cold Fargate
# start. max differs because ingest is the bulk path (someone uploads fifty
# files) and chat is one question at a time.
aws application-autoscaling register-scalable-target --region "$AWS_REGION" \
  --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
  --resource-id service/edgentrag-v3/edgentrag-v3-ingest --min-capacity 1 --max-capacity 3
aws application-autoscaling register-scalable-target --region "$AWS_REGION" \
  --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
  --resource-id service/edgentrag-v3/edgentrag-v3-chat   --min-capacity 1 --max-capacity 2

# IF(m2 < 1, m1, m1 / m2) guards the division: RunningTaskCount can briefly
# report 0 during a deployment, and m1/0 would produce no datapoint at all —
# leaving the policy blind exactly when the queue is backing up.
#
# ScaleOutCooldown 60 / ScaleInCooldown 300: react to a backlog quickly, shed
# capacity slowly. Asymmetric on purpose — scaling in too eagerly kills a task
# that was about to pick up the next message.
make_policy () {
  local SVC=$1 QUEUE=$2 TARGET=$3
  cat > /tmp/pol-$SVC.json <<EOF
{
  "TargetValue": ${TARGET},
  "ScaleInCooldown": 300,
  "ScaleOutCooldown": 60,
  "CustomizedMetricSpecification": {
    "Metrics": [
      {"Id": "m1", "ReturnData": false,
       "MetricStat": {"Stat": "Average", "Metric": {
         "Namespace": "AWS/SQS", "MetricName": "ApproximateNumberOfMessagesVisible",
         "Dimensions": [{"Name": "QueueName", "Value": "${QUEUE}"}]}}},
      {"Id": "m2", "ReturnData": false,
       "MetricStat": {"Stat": "Average", "Metric": {
         "Namespace": "ECS/ContainerInsights", "MetricName": "RunningTaskCount",
         "Dimensions": [{"Name": "ClusterName", "Value": "edgentrag-v3"},
                        {"Name": "ServiceName", "Value": "edgentrag-v3-${SVC}"}]}}},
      {"Id": "e1", "ReturnData": true,
       "Expression": "IF(m2 < 1, m1, m1 / m2)",
       "Label": "backlog per task"}
    ]
  }
}
EOF
  aws application-autoscaling put-scaling-policy --region "$AWS_REGION" \
    --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
    --resource-id "service/edgentrag-v3/edgentrag-v3-$SVC" \
    --policy-name "${SVC}-backlog-per-task" \
    --policy-type TargetTrackingScaling \
    --target-tracking-scaling-policy-configuration "file:///tmp/pol-$SVC.json"
}

make_policy ingest edgentrag-v3-ingest 2
make_policy chat   edgentrag-v3-chat   2

# ---------------------------------------------------------------------------
# Step 4 — verify
# ---------------------------------------------------------------------------
# The logs are the real proof. A healthy worker prints exactly two lines and
# then goes quiet, because it is long-polling an empty queue:
#
#   shared.db      database_url is not sqlite; schema is managed by Alembic
#   shared.worker  ingest: consuming edgentrag-v3-ingest
#
# That first line is worth pointing out — it is init_db() DECLINING to create
# tables, which is exactly the v2 behaviour that had to be removed before many
# tasks could start against shared RDS safely.
#
# Silence after the second line is success, not a hang. A worker that has
# nothing to do should say nothing.
aws logs tail /ecs/edgentrag-v3-ingest --region "$AWS_REGION" --since 10m
aws logs tail /ecs/edgentrag-v3-chat   --region "$AWS_REGION" --since 10m

aws ecs describe-services --region "$AWS_REGION" --cluster edgentrag-v3 \
  --services edgentrag-v3-ingest edgentrag-v3-chat \
  --query 'services[].{Name:serviceName,Running:runningCount,Desired:desiredCount,Rollout:deployments[?status==`PRIMARY`].rolloutState|[0]}' \
  --output table

# ---------------------------------------------------------------------------
# AN IAM DETAIL WORTH DEMONSTRATING
# ---------------------------------------------------------------------------
# When we fixed the malformed ARN above, the ALREADY-RUNNING task recovered on
# its own, without a restart. IAM policy changes take effect on the next
# credential refresh, and the task assumes its role continuously.
#
# That is NOT true of everything in a task definition. Change an `environment`
# value or a `secrets` reference and the running container keeps the old value
# until it is replaced — those are injected once, at container start.
#
#   IAM policy change      -> takes effect on a running task
#   env/secret change      -> needs `aws ecs update-service --force-new-deployment`
#
# Knowing which is which saves a lot of pointless redeploying, and a lot of
# "but I changed it" confusion.
#
# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
#   aws ecs update-service --region $AWS_REGION --cluster edgentrag-v3 --service edgentrag-v3-ingest --desired-count 0
#   aws ecs delete-service --region $AWS_REGION --cluster edgentrag-v3 --service edgentrag-v3-ingest --force
#   (same for chat)
#
# Setting desired-count 0 is also the cheap way to PAUSE billing between
# teaching sessions without destroying anything.
#
# ---------------------------------------------------------------------------
# NEXT: the EC2 Auto Scaling Group behind an ALB (DEPLOY.md 7) — the API and
# the frontend. That is the unit carrying the three settings that fail
# silently: health check on /api/health (not /ready), ALB idle timeout 300s,
# and ASG min 2.
