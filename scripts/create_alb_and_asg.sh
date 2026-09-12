#!/usr/bin/env bash
#
# Task 8 of the EdgentRAG v3 deployment — the API tier: ALB, launch template,
# Auto Scaling Group.
#
# Every command below was executed successfully in ap-southeast-1 on
# 2026-09-12. Nothing here is untested or aspirational — including the two
# failures, which are documented where they happened because both are more
# instructive than the happy path.
#
# WHAT THIS TIER IS
# -----------------
# Each instance runs TWO containers from docker-compose.ec2.yml:
#
#     web   nginx, serving the built React app, proxying /api -> api:8000
#     api   uvicorn, one worker
#
# Only 8080 is exposed to the host. The api container has no `ports:` at all —
# only nginx can reach it over the compose network, so port 8000 needs no
# security-group rule anywhere.
#
# Unlike the ECS workers, these images are NOT pulled from ECR. The compose
# file uses `build:`, so each instance builds them itself at boot. That keeps
# the deploy story to "git pull && docker compose up -d --build" but it makes
# bootstrap slow — which matters a great deal on the instance size the account
# quota forced on us (see step 5).
#
# HOW TO RUN
#   bash scripts/create_alb_and_asg.sh

set -euo pipefail

export AWS_REGION=ap-southeast-1
ACC=$(aws sts get-caller-identity --query Account --output text)
VPC=vpc-07a411d32015cc621
SG_ALB=sg-038544d1abfc26b09
SG_EC2=sg-073c71a2cc4551d9e
SUBNETS="subnet-0a466fb1f2d3a1e8f subnet-06ecb1c97f87a5545 subnet-0ac3b9285a4d3e530"

# ---------------------------------------------------------------------------
# Step 1 — the instance role, and the role/profile distinction
# ---------------------------------------------------------------------------
# An EC2 instance cannot be given an IAM ROLE directly. It is given an
# INSTANCE PROFILE, which is a container holding exactly one role. Two objects,
# usually the same name, and you must create both and link them. Forgetting
# add-role-to-instance-profile produces an instance whose credentials calls
# all fail with no obvious cause.
cat > /tmp/ec2-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

aws iam create-role --role-name edgentrag-v3-ec2 \
  --assume-role-policy-document file:///tmp/ec2-trust.json \
  --tags Key=Project,Value=edgentrag-v3

DB_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/database-url --query ARN --output text)
RD_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/redis-url    --query ARN --output text)
BT_ARN=$(aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id edgentrag-v3/broker-token --query ARN --output text)

# This role is where BROKER_TOKEN finally appears — and it is the ONLY place in
# our own infrastructure that needs it. The API verifies the GPU's bearer
# header in routes/broker.py; neither worker ever reads it. Compare with the
# ECS execution role from task 6, which deliberately omits it.
cat > /tmp/ec2-policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"Queues","Effect":"Allow",
  "Action":["sqs:SendMessage","sqs:GetQueueAttributes","sqs:ReceiveMessage","sqs:DeleteMessage","sqs:ChangeMessageVisibility"],
  "Resource":"arn:aws:sqs:${AWS_REGION}:${ACC}:edgentrag-v3-*"},
 {"Sid":"Bucket","Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],
  "Resource":"arn:aws:s3:::edgentrag-v3-${ACC}/*"},
 {"Sid":"Secrets","Effect":"Allow","Action":"secretsmanager:GetSecretValue",
  "Resource":["${DB_ARN}","${RD_ARN}","${BT_ARN}"]},
 {"Sid":"Parameters","Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],
  "Resource":"arn:aws:ssm:${AWS_REGION}:${ACC}:parameter/edgentrag-v3/*"}]}
EOF
aws iam put-role-policy --role-name edgentrag-v3-ec2 \
  --policy-name api-runtime --policy-document file:///tmp/ec2-policy.json

# SSM Session Manager: a shell on the instance with NO inbound port 22, no key
# pair, no bastion. DEPLOY.md suggests a key pair for troubleshooting; this is
# strictly better and is what we used to read the bootstrap log later.
aws iam attach-role-policy --role-name edgentrag-v3-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name edgentrag-v3-ec2
aws iam add-role-to-instance-profile --instance-profile-name edgentrag-v3-ec2 \
  --role-name edgentrag-v3-ec2

# ---------------------------------------------------------------------------
# Step 2 — the target group, and the health-check path that matters
# ---------------------------------------------------------------------------
# /api/health, NOT /api/ready. Both exist and they are deliberately different:
#
#   /health   liveness. Checks NOTHING external. "Is this process alive?"
#   /ready    readiness. Checks RDS, Redis, and all four SQS queues, and names
#             whichever one is broken.
#
# If the ALB checked /ready, a two-second RDS blip would fail EVERY instance's
# health check simultaneously, the ALB would deregister all of them, and a
# recoverable database hiccup would become a total outage — with the app
# refusing traffic for minutes after the database recovered. A shared
# dependency must never be in a per-instance health check.
#
# /ready is still valuable; it is for humans and dashboards, not load balancers.
aws elbv2 create-target-group --region "$AWS_REGION" \
  --name edgentrag-v3-api \
  --protocol HTTP --port 8080 --vpc-id "$VPC" \
  --target-type instance \
  --health-check-protocol HTTP --health-check-path /api/health \
  --health-check-interval-seconds 30 --health-check-timeout-seconds 5 \
  --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
  --matcher HttpCode=200

TG=$(aws elbv2 describe-target-groups --region "$AWS_REGION" --names edgentrag-v3-api \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

# ---------------------------------------------------------------------------
# Step 3 — the load balancer
# ---------------------------------------------------------------------------
aws elbv2 create-load-balancer --region "$AWS_REGION" \
  --name edgentrag-v3 --type application --scheme internet-facing \
  --subnets $SUBNETS --security-groups "$SG_ALB" \
  --tags Key=Project,Value=edgentrag-v3

ALB=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --names edgentrag-v3 \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# THE IDLE TIMEOUT. Default 60s. The SSE stream (routes/events.py) sends a
# keepalive every 15s, so 60 is survivable in theory — but any real gap, any
# slow event, and the ALB silently closes a connection the browser thought was
# healthy. The symptom is an EventSource that reconnects every minute or so,
# with nothing in any application log, because from the app's point of view
# nothing went wrong. 300s leaves real margin.
#
# Nothing in the app can detect or report this. It is pure infrastructure
# configuration that only shows up as degraded behaviour.
aws elbv2 modify-load-balancer-attributes --region "$AWS_REGION" \
  --load-balancer-arn "$ALB" \
  --attributes Key=idle_timeout.timeout_seconds,Value=300

ALB_CERT=$(aws acm list-certificates --region "$AWS_REGION" \
  --query "CertificateSummaryList[?DomainName=='study.edgent.in'].CertificateArn|[0]" --output text)

aws elbv2 create-listener --region "$AWS_REGION" --load-balancer-arn "$ALB" \
  --protocol HTTPS --port 443 \
  --certificates CertificateArn="$ALB_CERT" \
  --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \
  --default-actions Type=forward,TargetGroupArn="$TG"

aws elbv2 create-listener --region "$AWS_REGION" --load-balancer-arn "$ALB" \
  --protocol HTTP --port 80 \
  --default-actions '[{"Type":"redirect","RedirectConfig":{"Protocol":"HTTPS","Port":"443","StatusCode":"HTTP_301"}}]'

# A BUG WE SHIPPED AND THEN CAUGHT, worth reproducing deliberately.
#
# The security group from task 1 allowed only 443. So the port-80 listener
# above existed, was correctly configured, and was completely unreachable —
# anyone typing http://study.edgent.in got a HANG, not a redirect. curl just
# sat there until it timed out.
#
# A LISTENER AND ITS SECURITY GROUP MUST AGREE ON THE PORT. A listener on a
# blocked port is silently dead: the ALB console shows it as fine, because from
# the ALB's side it is fine. The packets never arrive.
#
# Test every port you configure, not just the one you expect people to use.
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG_ALB" \
  --ip-permissions 'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description="HTTP, redirected to 443 by the listener"}]'

# ---------------------------------------------------------------------------
# Step 4 — user data
# ---------------------------------------------------------------------------
# Written to /tmp/userdata.sh, base64'd into the launch template. Read the full
# file in this repo's history; the parts that matter:
#
# SWAP, FIRST, BEFORE ANYTHING ELSE:
#     fallocate -l 4G /swapfile && mkswap /swapfile && swapon /swapfile
#   This box has 2 GB of RAM and must run `npm install` + `vite build` + a pip
#   install at boot. Without swap the Node build is OOM-killed and docker
#   compose dies with exit 137 — a bare number, no message, nothing that points
#   at memory. With swap it completes (slowly). We watched it work.
#
# .env IS BUILT AT BOOT, from SSM + Secrets Manager, using the instance role:
#     DATABASE_URL=$(secret edgentrag-v3/database-url)
#     S3_BUCKET=$(param /edgentrag-v3/s3-bucket)
#   Nothing sensitive is in the AMI, the repo, or the launch template. Rotate a
#   secret and the next instance picks it up with no rebuild. This is the same
#   discipline as the ECS `secrets` block, done with shell instead of a task
#   definition.
#
# THE CODE COMES FROM A PUBLIC GIT CLONE:
#     git clone --depth 1 https://github.com/abhaykes1/edgentrag-v3.git app
#   Public specifically so user data needs no credential. A private repo would
#   mean a deploy key in Secrets Manager and three more steps here.

# ---------------------------------------------------------------------------
# Step 5 — the launch template, and TWO decisions the account forced
# ---------------------------------------------------------------------------
# DECISION A: IMDS HOP LIMIT MUST BE 2.
#
#   "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2}
#
# boto3 inside the api container fetches instance-role credentials from the
# metadata service at 169.254.169.254. From inside a Docker bridge network that
# is ONE EXTRA NETWORK HOP. The default hop limit of 1 drops it, and the
# container gets NoCredentialsError while `curl` on the host works perfectly —
# a maddening split. HttpTokens=required enforces IMDSv2, which is the right
# default anyway.
#
# DECISION B: t2.small, x86_64 — NOT the t4g.medium we wanted.
#
# The first launch template used t4g.medium (ARM, 2 vCPU), matching the
# Graviton choice from task 6. Every launch FAILED:
#
#   You have requested more vCPU capacity than your current vCPU limit of 1
#   allows for the instance bucket that the specified instance type belongs to
#
# This account's quota L-1216C47A ("Running On-Demand Standard instances") is
# ONE vCPU. Three things follow, and each is worth knowing:
#
#   1. `aws ec2 run-instances --dry-run` said "Request would have succeeded."
#      DRY-RUN VALIDATES PERMISSIONS AND PARAMETERS, NOT QUOTA. It is not proof
#      you can launch.
#
#   2. The failure appears ONLY in `describe-scaling-activities`. The ASG shows
#      zero instances and no error anywhere else. If an ASG is not launching,
#      that command is the first place to look — always.
#
#   3. NO ARM INSTANCE HAS FEWER THAN 2 vCPUs. t4g.nano is 2. So a 1-vCPU
#      budget forces x86: only t2.nano (0.5 GB), t2.micro (1 GB) and t2.small
#      (2 GB) qualify. t2.small is the only one with enough RAM to build the
#      images, and only with the swapfile above.
#
# Quotas are PER-REGION: a running instance in ap-south-1 does not consume
# ap-southeast-1's limit, and terminating it would not help. Request an
# increase instead:
#
#   aws service-quotas request-service-quota-increase --region $AWS_REGION \
#     --service-code ec2 --quota-code L-1216C47A --desired-value 16
#
# That came back CASE_OPENED — a human at AWS reviews it. Plan for hours.
AMI=$(aws ssm get-parameter --region "$AWS_REGION" \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameter.Value' --output text)   # resolved, never hardcoded: ami-xxxx goes stale

base64 -i /tmp/userdata.sh -o /tmp/userdata.b64
cat > /tmp/lt.json <<EOF
{
  "ImageId": "${AMI}",
  "InstanceType": "t2.small",
  "IamInstanceProfile": {"Name": "edgentrag-v3-ec2"},
  "SecurityGroupIds": ["${SG_EC2}"],
  "UserData": "$(tr -d '\n' < /tmp/userdata.b64)",
  "BlockDeviceMappings": [{"DeviceName": "/dev/sda1",
    "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": true}}],
  "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
  "TagSpecifications": [{"ResourceType": "instance",
    "Tags": [{"Key": "Name", "Value": "edgentrag-v3"}, {"Key": "Project", "Value": "edgentrag-v3"}]}]
}
EOF
aws ec2 create-launch-template --region "$AWS_REGION" \
  --launch-template-name edgentrag-v3 --launch-template-data file:///tmp/lt.json

# ---------------------------------------------------------------------------
# Step 6 — the Auto Scaling Group
# ---------------------------------------------------------------------------
# WHAT THIS SHOULD BE, and what DEPLOY.md correctly specifies:
#
#     --min-size 2 --max-size 4 --desired-capacity 2
#
# Two from the start is the ENTIRE REASON THE ALB EXISTS. With one instance an
# ALB is an expensive DNS name: a deploy is downtime, and an instance failure
# is an outage. The load balancer only earns its cost when there is something
# to balance across.
#
# WHAT WE ACTUALLY RAN, because of the 1 vCPU quota:
#
#     --min-size 1 --max-size 1 --desired-capacity 1
#
# max=1 as well as min=1, deliberately: leaving max at 4 would let a scaling
# policy try to add an instance that can never launch, generating a failed
# activity every few minutes forever.
#
# --health-check-type ELB, not EC2: EC2 health only asks "is the VM running",
# which stays true when the containers are dead. ELB health uses the target
# group's /api/health check, so a broken app gets the instance replaced.
#
# --health-check-grace-period 600: ten minutes before health checks count. Our
# bootstrap installs Docker and builds two images on ONE vCPU; the default 300
# would kill the instance mid-build and loop forever, replacing instances that
# were making perfectly good progress.
aws autoscaling create-auto-scaling-group --region "$AWS_REGION" \
  --auto-scaling-group-name edgentrag-v3 \
  --launch-template LaunchTemplateName=edgentrag-v3,Version='$Latest' \
  --min-size 1 --max-size 1 --desired-capacity 1 \
  --vpc-zone-identifier "$(echo $SUBNETS | tr ' ' ',')" \
  --target-group-arns "$TG" \
  --health-check-type ELB --health-check-grace-period 600 \
  --tags "Key=Project,Value=edgentrag-v3,PropagateAtLaunch=true"

# WHEN THE QUOTA INCREASE LANDS, this is the whole restoration — the launch
# template's version 1 (t4g.medium, arm64) is still there, untouched:
#
#   aws ec2 modify-launch-template --launch-template-name edgentrag-v3 --default-version 1
#   aws autoscaling update-auto-scaling-group --auto-scaling-group-name edgentrag-v3 \
#     --min-size 2 --max-size 4 --desired-capacity 2
#
# Two numbers and a version pointer. That the degraded and the intended
# deployments differ by so little is the point worth making.

# ---------------------------------------------------------------------------
# Step 7 — verify
# ---------------------------------------------------------------------------
# Watch the bootstrap WITHOUT SSH, via Session Manager:
#
#   aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript \
#     --parameters 'commands=["tail -30 /var/log/edgentrag-bootstrap.log"]'
#
# The user data redirects all output there AND to the console, so a boot that
# fails before SSM registers is still diagnosable via get-console-output.
aws elbv2 describe-target-health --region "$AWS_REGION" --target-group-arn "$TG" \
  --query 'TargetHealthDescriptions[].{Target:Target.Id,State:TargetHealth.State,Reason:TargetHealth.Reason}' \
  --output table

# On the instance, the two containers and a local health probe:
#   docker ps --format '{{.Names}} | {{.Status}}'
#     edgentrag-v3-web-1 | Up (healthy)
#     edgentrag-v3-api-1 | Up (healthy)
#   curl -s localhost:8080/api/health   ->  {"status":"ok","service":"api","env":"aws"}

# ---------------------------------------------------------------------------
# Teardown / pausing
# ---------------------------------------------------------------------------
#   aws autoscaling update-auto-scaling-group --auto-scaling-group-name edgentrag-v3 \
#     --min-size 0 --desired-capacity 0        # pause billing, keep config
#   aws autoscaling delete-auto-scaling-group --auto-scaling-group-name edgentrag-v3 --force-delete
#   aws elbv2 delete-load-balancer --load-balancer-arn $ALB
#   aws elbv2 delete-target-group  --target-group-arn $TG
#
# The ALB costs roughly $16-20/month just to exist, before any traffic. Setting
# the ASG to 0 does not stop that; deleting the load balancer does.
#
# ---------------------------------------------------------------------------
# NEXT: task 9 — the edge (WAF, CloudFront) and DNS.
