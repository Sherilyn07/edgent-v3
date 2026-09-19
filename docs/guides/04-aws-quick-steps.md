# AWS Deployment — Just The Steps

> The "why" for each step is in `03-aws-deployment-guide.md`. The full commands,
> with comments, are in `scripts/*.sh`. Run everything from the **repo root**
> in Git Bash or WSL.
>
> On Linux/Git Bash use `sed -i`, not the macOS `sed -i ''` that the scripts use.

---

## STEP 0: Setup

```bash
aws sts get-caller-identity              # right account?
export AWS_REGION=ap-southeast-1
ACC=$(aws sts get-caller-identity --query Account --output text)
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS="<3 subnet ids from: aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC>"
```

⚠️ Always write `${ACC}` with braces (zsh eats `$ACC:e`).

---

## STEP 1: Security groups
`scripts/create_security_groups.sh`

Create 5 empty groups in `$VPC`, tag them `Project=edgentrag-v3`, then add these rules:

```
edgentrag-v3-alb    inbound 443  from 0.0.0.0/0     (+ 80 in step 8)
edgentrag-v3-ec2    inbound 8080 from sg: alb
edgentrag-v3-ecs    NO inbound rules
edgentrag-v3-rds    inbound 5432 from sg: ec2, ecs
edgentrag-v3-redis  inbound 6379 from sg: ec2, ecs
```

---

## STEP 2: RDS + Redis + 2 secrets
`scripts/create_database_and_cache.sh`

```
Subnet groups:   edgentrag-v3-db (RDS), edgentrag-v3-cache (Redis) — all 3 subnets

RDS:             id edgentrag-v3 · db.t4g.micro · postgres 16.15
                 user edgentrag · db-name edgentrag · 20 GB gp3
                 NOT public · single-AZ · backup 1 day · sg rds

Redis:           replication-group edgentrag-v3 · cache.t4g.micro · redis 7.1
                 1 node · TLS in-transit + at-rest · sg redis
                 ⚠️ cluster mode OFF (don't pass --num-node-groups)
```

Wait for both:

```bash
aws rds wait db-instance-available --db-instance-identifier edgentrag-v3
aws elasticache wait replication-group-available --replication-group-id edgentrag-v3
```

Store the two URLs in Secrets Manager:

```
edgentrag-v3/database-url   postgresql://edgentrag:<pass>@<rds-host>:5432/edgentrag
edgentrag-v3/redis-url      rediss://<redis-primary-host>:6379/0
```

---

## STEP 3: S3 bucket
`scripts/create_s3_bucket.sh`

```
Bucket:      edgentrag-v3-${ACC}   (LocationConstraint=ap-southeast-1)
Public:      block all 4
Encryption:  AES256
CORS:        PUT · origins * · header Content-Type · expose ETag   ⚠️ uploads fail without it
SSM:         /edgentrag-v3/s3-bucket, /edgentrag-v3/aws-region
```

---

## STEP 4: SQS queues + broker token
`scripts/create_queues.sh`

```bash
python -m venv .venv && ./.venv/Scripts/pip install boto3 pydantic-settings   # Windows venv path
cp .env.example .env     # set S3_BUCKET and AWS_REGION=ap-southeast-1 FIRST
./.venv/Scripts/python -m scripts.bootstrap        # 8 queues + prints BROKER_TOKEN once
```

Store what it printed:

```
Secret:  edgentrag-v3/broker-token          = <BROKER_TOKEN>
SSM:     /edgentrag-v3/{ingest,chat,stt,embed}-queue-url
```

Check:

```bash
aws sqs list-queues --queue-name-prefix edgentrag-v3 --query 'length(QueueUrls)'   # → 8
```

---

## STEP 5: Cognito
`scripts/create_cognito.sh`

```
User pool:   edgentrag-v3 · username = email · auto-verify email
             password 8+, upper/lower/number · self sign-up ON
App client:  edgentrag-v3-web · ⚠️ NO client secret
             flows USER_SRP + REFRESH_TOKEN · ID 1h · refresh 30d
Group:       admins
```

Write the 3 values to **both** places:

```
SSM:                        /edgentrag-v3/cognito-{region,user-pool-id,app-client-id}
frontend/.env.production:   VITE_COGNITO_{REGION,USER_POOL_ID,APP_CLIENT_ID}
```

Check:

```bash
curl -s https://cognito-idp.$AWS_REGION.amazonaws.com/<POOL>/.well-known/jwks.json   # 2 keys
```

---

## STEP 6: Images, IAM, cluster, migrate
`scripts/create_images_and_migrate.sh`

### 6a. ECR + push

```bash
REG=$ACC.dkr.ecr.$AWS_REGION.amazonaws.com
for r in api ingest-worker chat-worker; do aws ecr create-repository --repository-name edgentrag-v3/$r --image-scanning-configuration scanOnPush=true; done
aws ecr get-login-password | docker login --username AWS --password-stdin $REG
docker build --platform linux/arm64 -f backend/Dockerfile        -t $REG/edgentrag-v3/api:latest .
docker build --platform linux/arm64 -f workers/chat/Dockerfile   -t $REG/edgentrag-v3/chat-worker:latest .
docker build --platform linux/arm64 -f workers/ingest/Dockerfile -t $REG/edgentrag-v3/ingest-worker:latest .
docker push ...   # all three, then verify with: aws ecr describe-images
```

### 6b. Cluster

```bash
aws ecs create-cluster --cluster-name edgentrag-v3 --capacity-providers FARGATE
```

### 6c. IAM roles (trust: `ecs-tasks.amazonaws.com`)

```
edgentrag-v3-ingest-task-role   SQS receive/delete/visibility on ingest · send on stt, embed · S3 get/put
edgentrag-v3-chat-task-role     SQS receive/delete/visibility on chat · nothing else
edgentrag-v3-execution-role     AmazonECSTaskExecutionRolePolicy + GetSecretValue(database-url, redis-url)
```

### 6d. Log groups

```
/ecs/edgentrag-v3-migrate
/ecs/edgentrag-v3-ingest
/ecs/edgentrag-v3-chat
```

### 6e. Migrate ⚠️ GATE — nothing else starts before this succeeds

```
Task def:  edgentrag-v3-migrate · api image · 0.5 vCPU / 1 GB · ARM64
           command python -m scripts.migrate · secret DATABASE_URL
```

```bash
aws ecs run-task --cluster edgentrag-v3 --launch-type FARGATE --task-definition edgentrag-v3-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[<3 subnets>],securityGroups=[<sg ecs>],assignPublicIp=ENABLED}"
aws logs tail /ecs/edgentrag-v3-migrate --since 10m    # expect: applying migrations up to head → done
```

---

## STEP 7: Worker services
`scripts/create_worker_services.sh`

```
Task def edgentrag-v3-ingest:  1 vCPU / 2 GB · 30 GB disk · ARM64 · stopTimeout 120
    secrets  DATABASE_URL, REDIS_URL
    env      ENV=aws, AWS_REGION, S3_BUCKET, INGEST_/STT_/EMBED_QUEUE_URL

Task def edgentrag-v3-chat:    0.5 vCPU / 1 GB · ARM64 · stopTimeout 60
    secrets  DATABASE_URL, REDIS_URL
    env      ENV=aws, AWS_REGION, CHAT_QUEUE_URL
```

```bash
# services: desired 1, FARGATE, sg ecs, assignPublicIp=ENABLED, --enable-execute-command
aws ecs create-service --cluster edgentrag-v3 --service-name edgentrag-v3-ingest --task-definition edgentrag-v3-ingest ...
aws ecs create-service --cluster edgentrag-v3 --service-name edgentrag-v3-chat   --task-definition edgentrag-v3-chat ...

# autoscaling
aws ecs update-cluster-settings --cluster edgentrag-v3 --settings name=containerInsights,value=enabled
#   ingest min 1 max 3 · chat min 1 max 2
#   target tracking on IF(tasks<1, msgs, msgs/tasks) = 2
#   cooldowns: out 60s, in 300s
```

Check: the logs say `consuming edgentrag-v3-ingest`, then go quiet.

---

## STEP 8: API tier — ALB + EC2 ASG
`scripts/create_alb_and_asg.sh`

### 8a. Instance role (trust: `ec2.amazonaws.com`)

```
Role:     edgentrag-v3-ec2
Allows:   SQS on edgentrag-v3-* · S3 get/put
          GetSecretValue on db-url, redis-url, broker-token · SSM get on /edgentrag-v3/*
          + AmazonSSMManagedInstanceCore
Profile:  instance profile edgentrag-v3-ec2  ← add the role to it
```

### 8b. Target group

```
Name:          edgentrag-v3-api · HTTP 8080 · target type instance
Health check:  /api/health (NOT /ready) · 30s interval · healthy 2 · unhealthy 3
```

### 8c. Load balancer

```
ALB:             edgentrag-v3 · internet-facing · 3 subnets · sg alb
Idle timeout:    300   ⚠️ default 60 kills live streams
Listener 443:    HTTPS · ACM cert (regional, from step 9) · TLS13-1-2-2021-06 → forward to the target group
Listener 80:     redirect → 443 (301)
Security group:  add 80 from 0.0.0.0/0 to sg alb   ⚠️ otherwise http:// hangs
```

### 8d. Launch template

```
Name:            edgentrag-v3
AMI:             Ubuntu 24.04 amd64 (resolve from the SSM public param)
Instance:        t2.small (quota) · 30 GB gp3
Profile / SG:    instance profile edgentrag-v3-ec2 · sg ec2
IMDS:            HttpTokens=required · HopLimit=2   ⚠️ else NoCredentialsError in the container
User data:       4G swap → install docker → git clone → build .env from SSM/Secrets
                 → docker compose -f docker-compose.ec2.yml up -d --build
```

### 8e. Auto Scaling Group

```
Name:          edgentrag-v3
Size:          min 1 · max 1 · desired 1   (intended: 2 / 4 / 2)
Placement:     3 subnets · attach the target group
Health:        type ELB · grace period 600
```

Check:

```bash
aws elbv2 describe-target-health --target-group-arn <TG>          # healthy
aws autoscaling describe-scaling-activities --auto-scaling-group-name edgentrag-v3   # if no instance appears
```

---

## STEP 9: Certificates, WAF, DNS
`scripts/create_edge_and_dns.sh`

```
ACM:        request study.edgent.in in us-east-1 (CloudFront) AND ap-southeast-1 (ALB)
            → 1 CNAME in Cloud DNS validates both

WAF:        rules Common + KnownBadInputs + IpReputation + rate limit 2000 / 5 min / IP
            REGIONAL ACL edgentrag-v3-alb → associate with the ALB   (retry if WAFUnavailableEntity)
            CLOUDFRONT ACL edgentrag-v3 (us-east-1) → for later

CloudFront: ❌ blocked (account verification). When unblocked:
            /api/sessions/*/events + /api/* → CachingDisabled + AllViewer
            default → CachingOptimized · origin timeout 60s

DNS:        CNAME study.edgent.in → <alb dns name>  (TTL 300)
```

Check:

```bash
curl -s https://study.edgent.in/api/health                  # {"status":"ok",...}
curl -s -o /dev/null -w "%{http_code}\n" http://study.edgent.in/api/health   # 301
curl -s -H "Authorization: Bearer nonsense" https://study.edgent.in/api/config/services
#   → "unreadable token" = good   ·   "missing bearer token" = header stripped
```

---

## STEP 10: Connect Colab + verify

```bash
# 1. sign up in the app, then:
aws cognito-idp admin-add-user-to-group --user-pool-id <POOL> --group-name admins --username <email>
#    → SIGN OUT AND BACK IN

# 2. on Colab, in services/.env:
#    BROKER_URL=https://study.edgent.in/api
#    BROKER_TOKEN=<same as Secrets Manager>
bash /content/drive/MyDrive/edgentrag_services/run_colab.sh

# 3. paste the 3 trycloudflare URLs into the app's first screen
# 4. upload a .txt + a short video, ask a question
```

---

## Pause (stop most of the bill)

```bash
aws autoscaling update-auto-scaling-group --auto-scaling-group-name edgentrag-v3 --min-size 0 --desired-capacity 0
aws ecs update-service --cluster edgentrag-v3 --service edgentrag-v3-ingest --desired-count 0
aws ecs update-service --cluster edgentrag-v3 --service edgentrag-v3-chat   --desired-count 0
# ALB, RDS, Redis still bill — delete them to stop completely
```

## Redeploy a code change

| Changed | Do |
|---|---|
| `workers/ingest/**` | rebuild + push → `aws ecs update-service --service edgentrag-v3-ingest --force-new-deployment` |
| `workers/chat/**` | same, with `edgentrag-v3-chat` |
| `backend/**`, `frontend/**` | `aws autoscaling start-instance-refresh --auto-scaling-group-name edgentrag-v3` |
| `shared/**` | **all three** of the above |
| `migrations/**` | rebuild the api image → re-run the step 6e task **before** deploying |
