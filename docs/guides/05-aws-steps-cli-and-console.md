# AWS Deployment — Every Step, CLI and Console

> Every step is shown **two ways**: first the **AWS CLI** commands, then the
> same thing clicked through the **AWS Console**. Pick one per step. You can
> mix them, because the result in AWS is identical.
>
> - Why each setting matters → `03-aws-deployment-guide.md`
> - The commented originals → `scripts/*.sh`
> - CLI: run from the **repo root** in **Git Bash** or WSL.
> - Console: **check the region in the top-right corner is `Asia Pacific (Singapore) ap-southeast-1`** before every step, except the ones that say us-east-1.

---

## STEP 0: Setup

### 💻 CLI

```bash
aws sts get-caller-identity                          # right account + user?
export AWS_REGION=ap-southeast-1
ACC=$(aws sts get-caller-identity --query Account --output text)
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC \
  --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone}' --output table
SUBNETS="subnet-aaa subnet-bbb subnet-ccc"          # ← paste the 3 ids from above
SUBNETS_CSV=$(echo $SUBNETS | tr ' ' ',')
```

⚠️ Always write `${ACC}` with braces. In zsh, `$ACC:e` silently eats text.

### 🖱️ Console

- Top-right account menu → note your **Account ID**.
- Region selector → **Asia Pacific (Singapore)**.
- **VPC → Your VPCs** → note the VPC marked **Default VPC = Yes**.
- **VPC → Subnets** → filter by that VPC → note the **3 subnet ids** (one per AZ).

---

## STEP 1: Security groups

### 💻 CLI

```bash
mk() { aws ec2 create-security-group --vpc-id $VPC --group-name $1 --description "$2" --query GroupId --output text; }
ALB=$(mk edgentrag-v3-alb   "Public entry point: the ALB")
EC2=$(mk edgentrag-v3-ec2   "EC2 instances running web + api")
ECS=$(mk edgentrag-v3-ecs   "Fargate workers")
RDS=$(mk edgentrag-v3-rds   "RDS PostgreSQL")
REDIS=$(mk edgentrag-v3-redis "ElastiCache Redis")
aws ec2 create-tags --resources $ALB $EC2 $ECS $RDS $REDIS --tags Key=Project,Value=edgentrag-v3

aws ec2 authorize-security-group-ingress --group-id $ALB \
  --ip-permissions 'IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]' \
                   'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]'
aws ec2 authorize-security-group-ingress --group-id $EC2 \
  --ip-permissions "IpProtocol=tcp,FromPort=8080,ToPort=8080,UserIdGroupPairs=[{GroupId=$ALB}]"
aws ec2 authorize-security-group-ingress --group-id $RDS \
  --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$EC2},{GroupId=$ECS}]"
aws ec2 authorize-security-group-ingress --group-id $REDIS \
  --ip-permissions "IpProtocol=tcp,FromPort=6379,ToPort=6379,UserIdGroupPairs=[{GroupId=$EC2},{GroupId=$ECS}]"
# ECS: no inbound rules, on purpose
```

### 🖱️ Console

**EC2 → Security Groups → Create security group**, 5 times. Create them all **empty first**, then add the rules. Select the **default VPC** every time.

```
Name                  Description                   Inbound rules
edgentrag-v3-alb      Public entry point: the ALB   (add below)
edgentrag-v3-ec2      EC2 instances web + api       (add below)
edgentrag-v3-ecs      Fargate workers               NONE — leave empty forever
edgentrag-v3-rds      RDS PostgreSQL                (add below)
edgentrag-v3-redis    ElastiCache Redis             (add below)
Outbound: leave the default "All traffic → 0.0.0.0/0"
Tags:     Project = edgentrag-v3
```

Then open each group → **Inbound rules → Edit inbound rules → Add rule**:

```
edgentrag-v3-alb    HTTPS      TCP 443   Source: Anywhere-IPv4 (0.0.0.0/0)
                    HTTP       TCP 80    Source: Anywhere-IPv4 (0.0.0.0/0)
edgentrag-v3-ec2    Custom TCP TCP 8080  Source: Custom → pick sg edgentrag-v3-alb
edgentrag-v3-rds    PostgreSQL TCP 5432  Source: Custom → sg edgentrag-v3-ec2
                    PostgreSQL TCP 5432  Source: Custom → sg edgentrag-v3-ecs
edgentrag-v3-redis  Custom TCP TCP 6379  Source: Custom → sg edgentrag-v3-ec2
                    Custom TCP TCP 6379  Source: Custom → sg edgentrag-v3-ecs
```

⚠️ Don't add SSH (22). Use Session Manager instead (step 8).

---

## STEP 2: RDS Postgres + ElastiCache Redis + 2 secrets

### 💻 CLI

```bash
aws rds create-db-subnet-group --db-subnet-group-name edgentrag-v3-db \
  --db-subnet-group-description "edgentrag-v3 RDS" --subnet-ids $SUBNETS
aws elasticache create-cache-subnet-group --cache-subnet-group-name edgentrag-v3-cache \
  --cache-subnet-group-description "edgentrag-v3 Redis" --subnet-ids $SUBNETS

LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32 > db-pass.txt   # letters+digits only

aws rds create-db-instance --db-instance-identifier edgentrag-v3 \
  --db-instance-class db.t4g.micro --engine postgres --engine-version 16.15 \
  --master-username edgentrag --master-user-password "$(cat db-pass.txt)" \
  --db-name edgentrag --allocated-storage 20 --storage-type gp3 \
  --db-subnet-group-name edgentrag-v3-db --vpc-security-group-ids $RDS \
  --no-publicly-accessible --backup-retention-period 1 --no-multi-az \
  --no-auto-minor-version-upgrade --tags Key=Project,Value=edgentrag-v3

aws elasticache create-replication-group --replication-group-id edgentrag-v3 \
  --replication-group-description "edgentrag-v3 pubsub + history" \
  --engine redis --engine-version 7.1 --cache-node-type cache.t4g.micro \
  --num-cache-clusters 1 --cache-subnet-group-name edgentrag-v3-cache \
  --security-group-ids $REDIS --transit-encryption-enabled --at-rest-encryption-enabled \
  --no-auto-minor-version-upgrade                   # no --num-node-groups = cluster mode OFF

aws rds wait db-instance-available --db-instance-identifier edgentrag-v3
aws elasticache wait replication-group-available --replication-group-id edgentrag-v3

RDS_HOST=$(aws rds describe-db-instances --db-instance-identifier edgentrag-v3 --query 'DBInstances[0].Endpoint.Address' --output text)
REDIS_HOST=$(aws elasticache describe-replication-groups --replication-group-id edgentrag-v3 --query 'ReplicationGroups[0].NodeGroups[0].PrimaryEndpoint.Address' --output text)

aws secretsmanager create-secret --name edgentrag-v3/database-url \
  --secret-string "postgresql://edgentrag:$(cat db-pass.txt)@${RDS_HOST}:5432/edgentrag"
aws secretsmanager create-secret --name edgentrag-v3/redis-url \
  --secret-string "rediss://${REDIS_HOST}:6379/0"
rm -f db-pass.txt
```

### 🖱️ Console

**2a. RDS subnet group:** RDS → **Subnet groups → Create DB subnet group**

```
Name:           edgentrag-v3-db
VPC:            default
AZs / Subnets:  select all 3
```

**2b. Database:** RDS → **Databases → Create database**

```
Creation method:            Standard create
Engine:                     PostgreSQL · version 16.15 (any 16.x ≥ 15 works)
Templates:                  Free tier  (or Dev/Test)
Availability:               Single-AZ DB instance
DB instance identifier:     edgentrag-v3
Master username:            edgentrag
Credentials management:     Self managed   ⚠️ NOT "Managed in Secrets Manager" (rotation breaks the URL)
Master password:            32 letters+digits, no symbols
Instance class:             Burstable → db.t4g.micro
Storage:                    gp3 · 20 GiB · UNCHECK "Enable storage autoscaling"
Connectivity:               Don't connect to an EC2 compute resource
VPC / Subnet group:         default / edgentrag-v3-db
Public access:              No
VPC security group:         Choose existing → edgentrag-v3-rds  (remove "default")
Additional configuration →
  Initial database name:    edgentrag     ⚠️ easy to miss, the app expects it
  Backup retention:         1 day
  Auto minor version upgrade: uncheck
```

Create, and wait about 10 min until **Available**. Then copy **Connectivity & security → Endpoint**.

**2c. Redis subnet group:** ElastiCache → **Subnet groups → Create subnet group**

```
Name:     edgentrag-v3-cache
VPC:      default
Subnets:  all 3
```

**2d. Redis:** ElastiCache → **Redis OSS caches → Create Redis OSS cache**

```
Deployment option:        Design your own cache → Standard create
Cluster mode:             ⚠️ Disabled   (live progress breaks silently if enabled)
Name:                     edgentrag-v3
Engine version:           7.1
Node type:                cache.t4g.micro
Number of replicas:       0
Multi-AZ / auto-failover: off
Subnet group:             edgentrag-v3-cache
Encryption at rest:       Enable
Encryption in transit:    Enable        (→ URL must be rediss://)
Security groups:          edgentrag-v3-redis
Auto upgrade minor:       off
```

Wait until **Available**, then copy the **Primary endpoint** (without `:6379`).

**2e. Two secrets:** Secrets Manager → **Store a new secret**, twice

```
Secret type:  Other type of secret → Plaintext tab → paste the value only
Name:         edgentrag-v3/database-url   value: postgresql://edgentrag:<pass>@<rds-endpoint>:5432/edgentrag
Name:         edgentrag-v3/redis-url      value: rediss://<redis-primary-endpoint>:6379/0
Rotation:     off
```

---

## STEP 3: S3 bucket

### 💻 CLI

```bash
BUCKET=edgentrag-v3-${ACC}
aws s3api create-bucket --bucket $BUCKET --create-bucket-configuration LocationConstraint=$AWS_REGION
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket $BUCKET --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
aws s3api put-bucket-cors --bucket $BUCKET --cors-configuration \
  '{"CORSRules":[{"AllowedMethods":["PUT"],"AllowedOrigins":["*"],"AllowedHeaders":["Content-Type"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3000}]}'
aws ssm put-parameter --name /edgentrag-v3/s3-bucket  --type String --value $BUCKET --overwrite
aws ssm put-parameter --name /edgentrag-v3/aws-region --type String --value $AWS_REGION --overwrite
```

### 🖱️ Console

**S3 → Create bucket**

```
Bucket type:           General purpose
Name:                  edgentrag-v3-<your-account-id>
Region:                ap-southeast-1
Object Ownership:      ACLs disabled
Block all public access: ✅ (all 4)
Versioning:            Disable
Default encryption:    SSE-S3 · Bucket Key: Enable
```

Then open the bucket → **Permissions → Cross-origin resource sharing (CORS) → Edit** → paste:

```json
[{"AllowedMethods":["PUT"],"AllowedOrigins":["*"],"AllowedHeaders":["Content-Type"],"ExposeHeaders":["ETag"],"MaxAgeSeconds":3000}]
```

⚠️ Without CORS, every upload fails with "upload failed — is the API running?"

**Systems Manager → Parameter Store → Create parameter**, twice:

```
/edgentrag-v3/s3-bucket    Standard · String · edgentrag-v3-<account-id>
/edgentrag-v3/aws-region   Standard · String · ap-southeast-1
```

---

## STEP 4: SQS queues + broker token

### 💻 CLI  (recommended: settings come from the app's own config)

```bash
python -m venv .venv && ./.venv/Scripts/pip install boto3 pydantic-settings   # Linux/WSL: .venv/bin/
cp .env.example .env
sed -i -e "s#^S3_BUCKET=.*#S3_BUCKET=$BUCKET#" -e "s#^AWS_REGION=.*#AWS_REGION=$AWS_REGION#" .env
./.venv/Scripts/python -m scripts.bootstrap      # creates 8 queues, prints 4 URLs + BROKER_TOKEN ONCE

aws secretsmanager create-secret --name edgentrag-v3/broker-token --secret-string "<BROKER_TOKEN>"
Q=https://sqs.${AWS_REGION}.amazonaws.com/${ACC}/edgentrag-v3
for n in ingest chat stt embed; do
  aws ssm put-parameter --name /edgentrag-v3/${n}-queue-url --type String --value "${Q}-${n}" --overwrite
done
aws sqs list-queues --queue-name-prefix edgentrag-v3 --query 'length(QueueUrls)'   # → 8
```

### 🖱️ Console

Do this for each of **ingest, chat, stt, embed** (8 queues in total). **Create the DLQ first**, because the main queue points at it.

**SQS → Create queue** (the DLQ):

```
Type:  Standard
Name:  edgentrag-v3-ingest-dlq      (all other settings default)
```

**SQS → Create queue** (the main queue):

```
Type:                        Standard
Name:                        edgentrag-v3-ingest
Visibility timeout:          15 minutes      (= 900 s)
Message retention period:    4 days
Receive message wait time:   20 seconds      (long polling)
Dead-letter queue:           Enabled → choose edgentrag-v3-ingest-dlq · Maximum receives: 5
```

⚠️ These numbers must match `shared/config.py`: `queue_visibility_seconds=900`, `queue_wait_seconds=20`, `queue_max_receives=5`. That's why the CLI path runs `bootstrap.py`, which reads them from there.

**Broker token.** Generate it locally (the console can't):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Secrets Manager → Store a new secret:**

```
Type:   Other type of secret → Plaintext
Name:   edgentrag-v3/broker-token
Value:  the token
```

**Parameter Store → Create parameter** × 4, each Standard · String, with the queue URL copied from the SQS queue's **Details → URL**:

```
/edgentrag-v3/ingest-queue-url
/edgentrag-v3/chat-queue-url
/edgentrag-v3/stt-queue-url
/edgentrag-v3/embed-queue-url
```

---

## STEP 5: Cognito

### 💻 CLI

```bash
POOL=$(aws cognito-idp create-user-pool --pool-name edgentrag-v3 \
  --username-attributes email --auto-verified-attributes email --mfa-configuration OFF \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
  --admin-create-user-config '{"AllowAdminCreateUserOnly":false}' \
  --account-recovery-setting '{"RecoveryMechanisms":[{"Priority":1,"Name":"verified_email"}]}' \
  --email-configuration '{"EmailSendingAccount":"COGNITO_DEFAULT"}' \
  --query UserPool.Id --output text)

CLIENT=$(aws cognito-idp create-user-pool-client --user-pool-id $POOL --client-name edgentrag-v3-web \
  --no-generate-secret --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --id-token-validity 1 --access-token-validity 1 --refresh-token-validity 30 \
  --token-validity-units '{"IdToken":"hours","AccessToken":"hours","RefreshToken":"days"}' \
  --prevent-user-existence-errors ENABLED --query UserPoolClient.ClientId --output text)

aws cognito-idp create-group --user-pool-id $POOL --group-name admins

aws ssm put-parameter --name /edgentrag-v3/cognito-region        --type String --value $AWS_REGION --overwrite
aws ssm put-parameter --name /edgentrag-v3/cognito-user-pool-id  --type String --value $POOL       --overwrite
aws ssm put-parameter --name /edgentrag-v3/cognito-app-client-id --type String --value $CLIENT     --overwrite

sed -i -e "s#^VITE_COGNITO_REGION=.*#VITE_COGNITO_REGION=$AWS_REGION#" \
       -e "s#^VITE_COGNITO_USER_POOL_ID=.*#VITE_COGNITO_USER_POOL_ID=$POOL#" \
       -e "s#^VITE_COGNITO_APP_CLIENT_ID=.*#VITE_COGNITO_APP_CLIENT_ID=$CLIENT#" frontend/.env.production
git commit -am "Point frontend at the Cognito pool" && git push     # EC2 builds from git
```

### 🖱️ Console

**Cognito → User pools → Create user pool.** The wizard's screen may differ slightly:

```
Application type:                Single-page application (SPA)   ← creates a client with NO secret
Application name:                edgentrag-v3-web
Sign-in identifiers:             Email
Self-registration:               ✅ Enable
Required attributes for sign-up: email
Return URL:                      leave empty (the app doesn't use the hosted login page)
```

Then **Create**, and open the new pool to adjust these settings:

- **Authentication methods → Password policy → Edit**
  ```
  Custom · min length 8 · uppercase ✅ · lowercase ✅ · numbers ✅ · special characters ❌
  ```
- **App clients → edgentrag-v3-web → Edit**
  ```
  Client secret:                   must say "no secret"   ⚠️ if it has one, delete the client and recreate as SPA
  Authentication flows:            ALLOW_USER_SRP_AUTH + ALLOW_REFRESH_TOKEN_AUTH only
  ID token expiration:             1 hour
  Refresh token expiration:        30 days
  Prevent user existence errors:   Enabled
  ```
- **Groups → Create group**
  ```
  Name: admins        (exactly this spelling)
  ```

Copy the **User pool ID** (pool overview) and the **Client ID** (App clients).

**Parameter Store → Create parameter** × 3 (Standard · String):

```
/edgentrag-v3/cognito-region          ap-southeast-1
/edgentrag-v3/cognito-user-pool-id    <pool id>
/edgentrag-v3/cognito-app-client-id   <client id>
```

Edit `frontend/.env.production` (the 3 `VITE_COGNITO_*` lines), then commit and push. **The website bakes these in when it's built.**

---

## STEP 6: Images, cluster, IAM, logs, migration

### 💻 CLI

**6a. ECR repos + images**

```bash
REG=$ACC.dkr.ecr.$AWS_REGION.amazonaws.com
for r in api ingest-worker chat-worker; do
  aws ecr create-repository --repository-name edgentrag-v3/$r --image-scanning-configuration scanOnPush=true
done
aws ecr get-login-password | docker login --username AWS --password-stdin $REG
docker build --platform linux/arm64 -f backend/Dockerfile        -t $REG/edgentrag-v3/api:latest .           && docker push $REG/edgentrag-v3/api:latest
docker build --platform linux/arm64 -f workers/chat/Dockerfile   -t $REG/edgentrag-v3/chat-worker:latest .   && docker push $REG/edgentrag-v3/chat-worker:latest
docker build --platform linux/arm64 -f workers/ingest/Dockerfile -t $REG/edgentrag-v3/ingest-worker:latest . && docker push $REG/edgentrag-v3/ingest-worker:latest
```

**6b. Cluster**

```bash
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com 2>/dev/null || true
aws ecs create-cluster --cluster-name edgentrag-v3 --capacity-providers FARGATE
```

**6c. IAM roles**

```bash
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > ecs-trust.json
for r in edgentrag-v3-ingest-task-role edgentrag-v3-chat-task-role edgentrag-v3-execution-role; do
  aws iam create-role --role-name $r --assume-role-policy-document file://ecs-trust.json
done
QA="arn:aws:sqs:${AWS_REGION}:${ACC}:edgentrag-v3"
aws iam put-role-policy --role-name edgentrag-v3-ingest-task-role --policy-name ingest-worker --policy-document "{
 \"Version\":\"2012-10-17\",\"Statement\":[
  {\"Effect\":\"Allow\",\"Action\":[\"sqs:ReceiveMessage\",\"sqs:DeleteMessage\",\"sqs:ChangeMessageVisibility\",\"sqs:GetQueueAttributes\"],\"Resource\":\"${QA}-ingest\"},
  {\"Effect\":\"Allow\",\"Action\":\"sqs:SendMessage\",\"Resource\":[\"${QA}-stt\",\"${QA}-embed\"]},
  {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::${BUCKET}/*\"}]}"
aws iam put-role-policy --role-name edgentrag-v3-chat-task-role --policy-name chat-worker --policy-document "{
 \"Version\":\"2012-10-17\",\"Statement\":[
  {\"Effect\":\"Allow\",\"Action\":[\"sqs:ReceiveMessage\",\"sqs:DeleteMessage\",\"sqs:ChangeMessageVisibility\",\"sqs:GetQueueAttributes\"],\"Resource\":\"${QA}-chat\"}]}"
aws iam attach-role-policy --role-name edgentrag-v3-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
DB_ARN=$(aws secretsmanager describe-secret --secret-id edgentrag-v3/database-url --query ARN --output text)
RD_ARN=$(aws secretsmanager describe-secret --secret-id edgentrag-v3/redis-url    --query ARN --output text)
aws iam put-role-policy --role-name edgentrag-v3-execution-role --policy-name read-deployment-secrets --policy-document "{
 \"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":[\"$DB_ARN\",\"$RD_ARN\"]}]}"
```

**6d. Log groups**

```bash
for g in migrate ingest chat; do aws logs create-log-group --log-group-name /ecs/edgentrag-v3-$g; done
```

**6e. The migration task (⚠️ a gate: nothing else starts before this succeeds)**

```bash
cat > migrate-taskdef.json <<EOF
{"family":"edgentrag-v3-migrate","requiresCompatibilities":["FARGATE"],"networkMode":"awsvpc",
 "cpu":"512","memory":"1024","runtimePlatform":{"cpuArchitecture":"ARM64","operatingSystemFamily":"LINUX"},
 "executionRoleArn":"arn:aws:iam::${ACC}:role/edgentrag-v3-execution-role",
 "containerDefinitions":[{"name":"api","image":"${REG}/edgentrag-v3/api:latest","essential":true,
   "command":["python","-m","scripts.migrate"],
   "secrets":[{"name":"DATABASE_URL","valueFrom":"${DB_ARN}"}],
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"/ecs/edgentrag-v3-migrate","awslogs-region":"${AWS_REGION}","awslogs-stream-prefix":"migrate"}}}]}
EOF
aws ecs register-task-definition --cli-input-json file://migrate-taskdef.json
aws ecs run-task --cluster edgentrag-v3 --launch-type FARGATE --task-definition edgentrag-v3-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS_CSV],securityGroups=[$ECS],assignPublicIp=ENABLED}"
aws logs tail /ecs/edgentrag-v3-migrate --since 10m --follow     # "applying migrations up to head" → "done"
```

### 🖱️ Console

**6a. ECR:** ECR → **Private registry → Repositories → Create repository**, 3 times

```
edgentrag-v3/api
edgentrag-v3/ingest-worker
edgentrag-v3/chat-worker
Scan on push: ✅
```

- ⚠️ **Building and pushing images needs Docker on your machine.** There's no console way to do it.
- Open a repo → **View push commands** → run those commands, but add `--platform linux/arm64` and build from the repo root with `-f <Dockerfile>` as in the CLI block.

**6b. Cluster:** ECS → **Clusters → Create cluster**

```
Name:            edgentrag-v3
Infrastructure:  AWS Fargate (serverless) only
Monitoring:      Container Insights ✅   (needed for autoscaling in step 7)
```

**6c. IAM roles:** IAM → **Roles → Create role**, 3 times

```
Trusted entity:  AWS service → Use case: Elastic Container Service → "Elastic Container Service Task"
```

| Role name | Permissions page | Then: **Add permissions → Create inline policy → JSON** |
|---|---|---|
| `edgentrag-v3-ingest-task-role` | none | SQS receive/delete/change-visibility/get-attrs on `…:edgentrag-v3-ingest` · SendMessage on `-stt`, `-embed` · S3 Get/PutObject on `arn:aws:s3:::edgentrag-v3-<acc>/*` |
| `edgentrag-v3-chat-task-role` | none | SQS receive/delete/change-visibility/get-attrs on `…:edgentrag-v3-chat` **only** |
| `edgentrag-v3-execution-role` | ✅ `AmazonECSTaskExecutionRolePolicy` | `secretsmanager:GetSecretValue` on the database-url + redis-url secret ARNs |

The JSON for each inline policy is the same as in the CLI block, with your account id filled in.

**6d. Log groups:** CloudWatch → **Log groups → Create log group**, 3 times

```
/ecs/edgentrag-v3-migrate
/ecs/edgentrag-v3-ingest
/ecs/edgentrag-v3-chat
```

**6e. Migration task definition:** ECS → **Task definitions → Create new task definition**

```
Family:              edgentrag-v3-migrate
Launch type:         AWS Fargate
OS / Architecture:   Linux / ARM64          ⚠️ must match the image
CPU / Memory:        .5 vCPU / 1 GB
Task role:           none
Execution role:      edgentrag-v3-execution-role
Container name:      api
Image URI:           <acc>.dkr.ecr.ap-southeast-1.amazonaws.com/edgentrag-v3/api:latest
Port mappings:       remove all
Environment variables → Add → Key DATABASE_URL · Value type: ValueFrom · Value: <database-url secret ARN>
Docker configuration → Command:  python,-m,scripts.migrate
Log collection:      ✅ awslogs · group /ecs/edgentrag-v3-migrate
```

Tip: **Create new task definition with JSON** lets you paste the JSON from the CLI block instead.

**Run it:** ECS → Clusters → edgentrag-v3 → **Tasks → Run new task**

```
Compute options:   Launch type → FARGATE
Application type:  Task · Family edgentrag-v3-migrate
Networking:        default VPC · all 3 subnets · SG edgentrag-v3-ecs (remove default) · Public IP: ON ⚠️
```

Open the task → **Logs** → you should see `applying migrations up to head` → `done`.

---

## STEP 7: Worker services + autoscaling

### 💻 CLI

```bash
QURL=https://sqs.${AWS_REGION}.amazonaws.com/${ACC}/edgentrag-v3
taskdef() {   # name cpu mem stop taskrole envjson extra
cat > $1-taskdef.json <<EOF
{"family":"edgentrag-v3-$1","requiresCompatibilities":["FARGATE"],"networkMode":"awsvpc",
 "cpu":"$2","memory":"$3",$7
 "runtimePlatform":{"cpuArchitecture":"ARM64","operatingSystemFamily":"LINUX"},
 "executionRoleArn":"arn:aws:iam::${ACC}:role/edgentrag-v3-execution-role",
 "taskRoleArn":"arn:aws:iam::${ACC}:role/$5",
 "containerDefinitions":[{"name":"$1-worker","image":"${REG}/edgentrag-v3/$1-worker:latest","essential":true,
   "stopTimeout":$4,
   "secrets":[{"name":"DATABASE_URL","valueFrom":"${DB_ARN}"},{"name":"REDIS_URL","valueFrom":"${RD_ARN}"}],
   "environment":$6,
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"/ecs/edgentrag-v3-$1","awslogs-region":"${AWS_REGION}","awslogs-stream-prefix":"$1"}}}]}
EOF
aws ecs register-task-definition --cli-input-json file://$1-taskdef.json
}
taskdef ingest 1024 2048 120 edgentrag-v3-ingest-task-role \
  "[{\"name\":\"ENV\",\"value\":\"aws\"},{\"name\":\"AWS_REGION\",\"value\":\"$AWS_REGION\"},{\"name\":\"S3_BUCKET\",\"value\":\"$BUCKET\"},{\"name\":\"INGEST_QUEUE_URL\",\"value\":\"$QURL-ingest\"},{\"name\":\"STT_QUEUE_URL\",\"value\":\"$QURL-stt\"},{\"name\":\"EMBED_QUEUE_URL\",\"value\":\"$QURL-embed\"}]" \
  '"ephemeralStorage":{"sizeInGiB":30},'
taskdef chat 512 1024 60 edgentrag-v3-chat-task-role \
  "[{\"name\":\"ENV\",\"value\":\"aws\"},{\"name\":\"AWS_REGION\",\"value\":\"$AWS_REGION\"},{\"name\":\"CHAT_QUEUE_URL\",\"value\":\"$QURL-chat\"}]" ""

NET="awsvpcConfiguration={subnets=[$SUBNETS_CSV],securityGroups=[$ECS],assignPublicIp=ENABLED}"
for s in ingest chat; do
  aws ecs create-service --cluster edgentrag-v3 --service-name edgentrag-v3-$s --task-definition edgentrag-v3-$s \
    --desired-count 1 --launch-type FARGATE --network-configuration "$NET" --enable-execute-command
done

aws ecs update-cluster-settings --cluster edgentrag-v3 --settings name=containerInsights,value=enabled
for s in "ingest 3" "chat 2"; do set -- $s
  aws application-autoscaling register-scalable-target --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id service/edgentrag-v3/edgentrag-v3-$1 \
    --min-capacity 1 --max-capacity $2
  aws application-autoscaling put-scaling-policy --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id service/edgentrag-v3/edgentrag-v3-$1 \
    --policy-name $1-backlog-per-task --policy-type TargetTrackingScaling \
    --target-tracking-scaling-policy-configuration "{\"TargetValue\":2,\"ScaleInCooldown\":300,\"ScaleOutCooldown\":60,
     \"CustomizedMetricSpecification\":{\"Metrics\":[
      {\"Id\":\"m1\",\"ReturnData\":false,\"MetricStat\":{\"Stat\":\"Average\",\"Metric\":{\"Namespace\":\"AWS/SQS\",\"MetricName\":\"ApproximateNumberOfMessagesVisible\",\"Dimensions\":[{\"Name\":\"QueueName\",\"Value\":\"edgentrag-v3-$1\"}]}}},
      {\"Id\":\"m2\",\"ReturnData\":false,\"MetricStat\":{\"Stat\":\"Average\",\"Metric\":{\"Namespace\":\"ECS/ContainerInsights\",\"MetricName\":\"RunningTaskCount\",\"Dimensions\":[{\"Name\":\"ClusterName\",\"Value\":\"edgentrag-v3\"},{\"Name\":\"ServiceName\",\"Value\":\"edgentrag-v3-$1\"}]}}},
      {\"Id\":\"e1\",\"ReturnData\":true,\"Expression\":\"IF(m2 < 1, m1, m1 / m2)\",\"Label\":\"backlog per task\"}]}}"
done
aws logs tail /ecs/edgentrag-v3-ingest --since 10m     # "consuming edgentrag-v3-ingest", then silence = healthy
```

### 🖱️ Console

**7a. Two task definitions:** ECS → **Task definitions → Create new task definition** (or paste the JSON)

| Field | ingest | chat |
|---|---|---|
| Family | `edgentrag-v3-ingest` | `edgentrag-v3-chat` |
| Launch type · OS/Arch | Fargate · Linux/**ARM64** | Fargate · Linux/**ARM64** |
| CPU / Memory | 1 vCPU / 2 GB | .5 vCPU / 1 GB |
| Ephemeral storage | **30** GiB | default (21) |
| Task role | `edgentrag-v3-ingest-task-role` | `edgentrag-v3-chat-task-role` |
| Execution role | `edgentrag-v3-execution-role` | same |
| Container name | `ingest-worker` | `chat-worker` |
| Image | `…/edgentrag-v3/ingest-worker:latest` | `…/edgentrag-v3/chat-worker:latest` |
| Port mappings | none | none |
| Env (ValueFrom, secrets) | `DATABASE_URL`, `REDIS_URL` → secret ARNs | same |
| Env (Value) | `ENV=aws`, `AWS_REGION`, `S3_BUCKET`, `INGEST_QUEUE_URL`, `STT_QUEUE_URL`, `EMBED_QUEUE_URL` | `ENV=aws`, `AWS_REGION`, `CHAT_QUEUE_URL` |
| Stop timeout (Container → Timeouts) | 120 | 60 |
| Logs | awslogs · `/ecs/edgentrag-v3-ingest` | `/ecs/edgentrag-v3-chat` |

**7b. Two services:** ECS → Clusters → edgentrag-v3 → **Services → Create**

```
Compute options:     Launch type · FARGATE · Platform LATEST
Application type:    Service
Family:              edgentrag-v3-ingest   (then again for chat)
Service name:        edgentrag-v3-ingest   /   edgentrag-v3-chat
Desired tasks:       1
Networking:          default VPC · 3 subnets · SG edgentrag-v3-ecs only · Public IP: ON
Load balancing:      None
```

**7c. Autoscaling:**

- The console's service auto scaling only offers **CPU / memory / ALB request** targets.
- The "queue messages ÷ running tasks" metric we use needs **metric math**, which is **CLI only**. Run the `register-scalable-target` + `put-scaling-policy` part of the CLI block.
- A simpler console-only option: **Update service → Service auto scaling** → min 1 / max 3 → **Step scaling** on a CloudWatch alarm for `ApproximateNumberOfMessagesVisible` > 2. It works, but it scales on raw queue depth, which is less precise.

**Check:** the task's **Logs** tab shows `consuming edgentrag-v3-ingest`, then nothing. That silence is healthy.

---

## STEP 8: API tier — ALB + EC2 Auto Scaling Group

> ⚠️ **The HTTPS listener needs the regional certificate.** Do **9a (ACM ap-southeast-1)** first, then come back.

### 💻 CLI

**8a. Instance role + profile**

```bash
echo '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > ec2-trust.json
aws iam create-role --role-name edgentrag-v3-ec2 --assume-role-policy-document file://ec2-trust.json
BT_ARN=$(aws secretsmanager describe-secret --secret-id edgentrag-v3/broker-token --query ARN --output text)
aws iam put-role-policy --role-name edgentrag-v3-ec2 --policy-name api-runtime --policy-document "{
 \"Version\":\"2012-10-17\",\"Statement\":[
  {\"Effect\":\"Allow\",\"Action\":[\"sqs:SendMessage\",\"sqs:GetQueueAttributes\",\"sqs:ReceiveMessage\",\"sqs:DeleteMessage\",\"sqs:ChangeMessageVisibility\"],\"Resource\":\"arn:aws:sqs:${AWS_REGION}:${ACC}:edgentrag-v3-*\"},
  {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::${BUCKET}/*\"},
  {\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":[\"$DB_ARN\",\"$RD_ARN\",\"$BT_ARN\"]},
  {\"Effect\":\"Allow\",\"Action\":[\"ssm:GetParameter\",\"ssm:GetParameters\",\"ssm:GetParametersByPath\"],\"Resource\":\"arn:aws:ssm:${AWS_REGION}:${ACC}:parameter/edgentrag-v3/*\"}]}"
aws iam attach-role-policy --role-name edgentrag-v3-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name edgentrag-v3-ec2
aws iam add-role-to-instance-profile --instance-profile-name edgentrag-v3-ec2 --role-name edgentrag-v3-ec2
```

**8b. Target group + ALB + listeners**

```bash
TG=$(aws elbv2 create-target-group --name edgentrag-v3-api --protocol HTTP --port 8080 --vpc-id $VPC \
  --target-type instance --health-check-path /api/health --health-check-interval-seconds 30 \
  --health-check-timeout-seconds 5 --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
  --matcher HttpCode=200 --query 'TargetGroups[0].TargetGroupArn' --output text)
ALB_ARN=$(aws elbv2 create-load-balancer --name edgentrag-v3 --type application --scheme internet-facing \
  --subnets $SUBNETS --security-groups $ALB --query 'LoadBalancers[0].LoadBalancerArn' --output text)
aws elbv2 modify-load-balancer-attributes --load-balancer-arn $ALB_ARN --attributes Key=idle_timeout.timeout_seconds,Value=300
ALB_CERT=$(aws acm list-certificates --query "CertificateSummaryList[?DomainName=='study.edgent.in'].CertificateArn|[0]" --output text)
aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTPS --port 443 \
  --certificates CertificateArn=$ALB_CERT --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \
  --default-actions Type=forward,TargetGroupArn=$TG
aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTP --port 80 \
  --default-actions '[{"Type":"redirect","RedirectConfig":{"Protocol":"HTTPS","Port":"443","StatusCode":"HTTP_301"}}]'
```

**8c. User data.** Save as `userdata.sh`. This is a reconstruction; the original isn't in the repo.

```bash
#!/bin/bash
exec > >(tee /var/log/edgentrag-bootstrap.log) 2>&1
set -euxo pipefail
REGION=ap-southeast-1
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile   # 2 GB box needs it
curl -fsSL https://get.docker.com | sh
snap install aws-cli --classic
mkdir -p /opt/edgentrag && cd /opt/edgentrag
git clone --depth 1 https://github.com/abhaykes1/edgentrag-v3.git app && cd app
param()  { aws ssm get-parameter --region $REGION --name "$1" --query Parameter.Value --output text; }
secret() { aws secretsmanager get-secret-value --region $REGION --secret-id "$1" --query SecretString --output text; }
cat > .env <<EOF
ENV=aws
DATABASE_URL=$(secret edgentrag-v3/database-url)
REDIS_URL=$(secret edgentrag-v3/redis-url)
BROKER_TOKEN=$(secret edgentrag-v3/broker-token)
S3_BUCKET=$(param /edgentrag-v3/s3-bucket)
AWS_REGION=$REGION
INGEST_QUEUE_URL=$(param /edgentrag-v3/ingest-queue-url)
CHAT_QUEUE_URL=$(param /edgentrag-v3/chat-queue-url)
STT_QUEUE_URL=$(param /edgentrag-v3/stt-queue-url)
EMBED_QUEUE_URL=$(param /edgentrag-v3/embed-queue-url)
COGNITO_REGION=$(param /edgentrag-v3/cognito-region)
COGNITO_USER_POOL_ID=$(param /edgentrag-v3/cognito-user-pool-id)
COGNITO_APP_CLIENT_ID=$(param /edgentrag-v3/cognito-app-client-id)
WEB_PORT=8080
EOF
chmod 600 .env
docker compose -f docker-compose.ec2.yml up -d --build
```

**8d. Launch template + ASG**

```bash
AMI=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
cat > lt.json <<EOF
{"ImageId":"$AMI","InstanceType":"t2.small","IamInstanceProfile":{"Name":"edgentrag-v3-ec2"},
 "SecurityGroupIds":["$EC2"],"UserData":"$(base64 -w0 userdata.sh)",
 "BlockDeviceMappings":[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}],
 "MetadataOptions":{"HttpTokens":"required","HttpPutResponseHopLimit":2},
 "TagSpecifications":[{"ResourceType":"instance","Tags":[{"Key":"Name","Value":"edgentrag-v3"},{"Key":"Project","Value":"edgentrag-v3"}]}]}
EOF
aws ec2 create-launch-template --launch-template-name edgentrag-v3 --launch-template-data file://lt.json
aws autoscaling create-auto-scaling-group --auto-scaling-group-name edgentrag-v3 \
  --launch-template "LaunchTemplateName=edgentrag-v3,Version=\$Latest" \
  --min-size 1 --max-size 1 --desired-capacity 1 --vpc-zone-identifier "$SUBNETS_CSV" \
  --target-group-arns $TG --health-check-type ELB --health-check-grace-period 600
aws elbv2 describe-target-health --target-group-arn $TG        # wait ~10 min → healthy
aws autoscaling describe-scaling-activities --auto-scaling-group-name edgentrag-v3   # if no instance appears
```

### 🖱️ Console

**8a. Instance role:** IAM → **Roles → Create role**

```
Trusted entity:  AWS service → EC2
Permissions:     ✅ AmazonSSMManagedInstanceCore
Name:            edgentrag-v3-ec2
```

Then open the role → **Add permissions → Create inline policy → JSON** → paste the `api-runtime` policy from the CLI block.

The console creates the matching **instance profile** automatically. The CLI doesn't.

**8b. Target group:** EC2 → **Target groups → Create target group**

```
Target type:        Instances
Name:               edgentrag-v3-api
Protocol / Port:    HTTP / 8080
VPC:                default
Health check path:  /api/health        ⚠️ NOT /api/ready
Advanced:           healthy 2 · unhealthy 3 · timeout 5 · interval 30 · success codes 200
Register targets:   skip (the ASG does it)
```

**8c. Load balancer:** EC2 → **Load balancers → Create → Application Load Balancer**

```
Name:            edgentrag-v3
Scheme:          Internet-facing · IPv4
VPC / Mappings:  default · tick all 3 AZs
Security groups: edgentrag-v3-alb only (remove default)
Listener:        HTTPS : 443 → Forward to edgentrag-v3-api
Secure listener: Security policy ELBSecurityPolicy-TLS13-1-2-2021-06 · Certificate: From ACM → study.edgent.in
```

After it's created, open the ALB:

- **Listeners → Add listener**
  ```
  HTTP : 80 → Redirect to URL → HTTPS · 443 · 301
  ```
- **Attributes → Edit**
  ```
  Connection idle timeout: 300 seconds   ⚠️ default 60 silently cuts live streams
  ```

**8d. Launch template:** EC2 → **Launch templates → Create launch template**

```
Name:               edgentrag-v3
AMI:                Ubuntu Server 24.04 LTS · 64-bit (x86)
Instance type:      t2.small   (t4g.medium once your vCPU quota allows)
Key pair:           Don't include
Security groups:    edgentrag-v3-ec2
Storage:            30 GiB gp3
Resource tags:      Name = edgentrag-v3 · Project = edgentrag-v3
Advanced details →
  IAM instance profile:        edgentrag-v3-ec2
  Metadata version:            V2 only (token required)
  Metadata response hop limit: 2        ⚠️ else the container gets NoCredentialsError
  User data:                   paste userdata.sh from the CLI block
```

**8e. Auto Scaling group:** EC2 → **Auto Scaling groups → Create**

```
Name:                 edgentrag-v3
Launch template:      edgentrag-v3 · Latest
VPC / subnets:        default · all 3
Load balancing:       Attach to an existing load balancer → target group edgentrag-v3-api
Health checks:        ✅ Turn on Elastic Load Balancing health checks · grace period 600 s
Group size:           desired 1 · min 1 · max 1   (intended 2 / 2 / 4 once quota allows)
Scaling policies:     None
Tags:                 Project = edgentrag-v3
```

**Check:** Target group → **Targets** tab → `healthy` after about 10 min.

- If no instance appears: ASG → **Activity** tab. That's where quota errors show up, and nowhere else.
- To see the boot log: EC2 → instance → **Connect → Session Manager** → `sudo tail -50 /var/log/edgentrag-bootstrap.log`.

---

## STEP 9: Certificates, WAF, (CloudFront), DNS

### 💻 CLI

**9a. Certificates** (the ALB's one must exist before step 8's listener)

```bash
DOMAIN=study.edgent.in
aws acm request-certificate --region $AWS_REGION --domain-name $DOMAIN --validation-method DNS   # for the ALB
aws acm request-certificate --region us-east-1   --domain-name $DOMAIN --validation-method DNS   # for CloudFront
ALB_CERT=$(aws acm list-certificates --region $AWS_REGION --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn|[0]" --output text)
aws acm describe-certificate --region $AWS_REGION --certificate-arn $ALB_CERT \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'     # → Name + Value of 1 CNAME
gcloud dns record-sets create "<Name>" --zone=edgent-in-zone --project=edgent-app-prod \
  --type=CNAME --ttl=300 --rrdatas="<Value>"                           # the same CNAME validates BOTH certs
aws acm wait certificate-validated --region $AWS_REGION --certificate-arn $ALB_CERT
```

**9b. WAF** (regional, on the ALB)

```bash
cat > waf-rules.json <<'EOF'
[{"Name":"AWSCommon","Priority":1,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesCommonRuleSet"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSCommon"}},
 {"Name":"AWSKnownBadInputs","Priority":2,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesKnownBadInputsRuleSet"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSKnownBadInputs"}},
 {"Name":"AWSIpReputation","Priority":3,"Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesAmazonIpReputationList"}},"OverrideAction":{"None":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSIpReputation"}},
 {"Name":"RateLimitPerIP","Priority":4,"Statement":{"RateBasedStatement":{"Limit":2000,"AggregateKeyType":"IP"}},"Action":{"Block":{}},"VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"RateLimitPerIP"}}]
EOF
WAF=$(aws wafv2 create-web-acl --scope REGIONAL --name edgentrag-v3-alb --default-action Allow={} \
  --rules file://waf-rules.json \
  --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=edgentragV3Alb \
  --query Summary.ARN --output text)
aws wafv2 associate-web-acl --web-acl-arn $WAF --resource-arn $ALB_ARN   # retry if WAFUnavailableEntityException
```

**9c. CloudFront:** ❌ blocked on a new account (`AccessDenied … must be verified`). Open an AWS Support case first. When unblocked, see 9c in the console section for the settings.

**9d. DNS**

```bash
ALB_DNS=$(aws elbv2 describe-load-balancers --names edgentrag-v3 --query 'LoadBalancers[0].DNSName' --output text)
gcloud dns record-sets create "study.edgent.in." --zone=edgent-in-zone --project=edgent-app-prod \
  --type=CNAME --ttl=300 --rrdatas="${ALB_DNS}."
curl -s https://study.edgent.in/api/health                                                   # {"status":"ok",...}
curl -s -H "Authorization: Bearer nonsense" https://study.edgent.in/api/config/services      # "unreadable token" = good
```

### 🖱️ Console

**9a. Certificates:** Certificate Manager → **Request certificate**, **twice**: once with the region set to **ap-southeast-1** and once with **us-east-1**.

```
Type:               Request a public certificate
FQDN:               study.edgent.in
Validation method:  DNS validation
Key algorithm:      RSA 2048
```

- Open the certificate → **Domains** → copy the **CNAME name** and **CNAME value**. They're the same for both certificates.
- **Google Cloud Console → Network services → Cloud DNS → edgent-in-zone → Add standard record set**
  ```
  DNS name:  <CNAME name, without the zone part>
  Type:      CNAME
  TTL:       300
  Data:      <CNAME value>
  ```
- Wait until both certificates say **Issued** (usually a few minutes).

**9b. WAF:** WAF & Shield → **Web ACLs** (region ap-southeast-1) → **Create web ACL**

```
Resource type:         Regional resources
Region:                Asia Pacific (Singapore)
Name:                  edgentrag-v3-alb
Associated resources:  Add → Application Load Balancer → edgentrag-v3
Rules → Add managed rule groups → AWS managed rule groups:
    ✅ Core rule set  ✅ Known bad inputs  ✅ Amazon IP reputation list
Rules → Add my own rules → Rule builder:
    Name RateLimitPerIP · Type Rate-based rule · Rate limit 2000 · Evaluation window 5 min
    Request aggregation: Source IP address · Action: Block
Default action:        Allow
```

**9c. CloudFront** (once your account is verified). CloudFront → **Create distribution**

```
Origin:                   the ALB (edgentrag-v3-…elb.amazonaws.com) · HTTPS only · Origin response timeout 60
Default behavior:         Redirect HTTP→HTTPS · Cache policy CachingOptimized
Add behavior /api/*:                  CachingDisabled · Origin request policy AllViewer  ⚠️
Add behavior /api/sessions/*/events:  CachingDisabled · AllViewer · Compress OFF  ⚠️
Alternate domain name:    study.edgent.in · Custom SSL cert: the us-east-1 one
WAF:                      a CLOUDFRONT-scope web ACL (create it in us-east-1)
```

Then point the DNS CNAME at the `dxxxx.cloudfront.net` domain. Then change the ALB security group's 443 source to the **CloudFront managed prefix list**.

**9d. DNS:** Google Cloud DNS → **edgent-in-zone → Add standard record set**

```
DNS name:  study
Type:      CNAME
TTL:       300
Data:      <ALB DNS name from EC2 → Load balancers>.
```

**Check:** open `https://study.edgent.in/api/health` in a browser, which should show `{"status":"ok",...}`.

---

## STEP 10: Admin user + connect Colab

### 💻 CLI

```bash
# 1. sign up at https://study.edgent.in and enter the emailed code, then:
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL --group-name admins --username <your-email>
# 2. sign OUT and back IN (the group lives inside your token)
# 3. on Colab, services/.env:  BROKER_URL=https://study.edgent.in/api   BROKER_TOKEN=<same as the secret>
aws secretsmanager get-secret-value --secret-id edgentrag-v3/broker-token --query SecretString --output text
bash /content/drive/MyDrive/edgentrag_services/run_colab.sh
# 4. paste the 3 trycloudflare URLs into the app's first screen, upload a .txt + a short video, ask a question
```

### 🖱️ Console

1. Sign up in the app and confirm the emailed code.
2. **Cognito → User pools → edgentrag-v3 → Users** → click your email → **Add user to group** → `admins`.
3. **Sign out and back in.**
4. **Secrets Manager → edgentrag-v3/broker-token → Retrieve secret value** → copy it into `services/.env` on Colab.
5. On Colab, run `run_colab.sh`, then paste the 3 URLs into the app.

---

## Pause / resume (both ways)

**💻 CLI:**

```bash
aws autoscaling update-auto-scaling-group --auto-scaling-group-name edgentrag-v3 --min-size 0 --desired-capacity 0
aws ecs update-service --cluster edgentrag-v3 --service edgentrag-v3-ingest --desired-count 0
aws ecs update-service --cluster edgentrag-v3 --service edgentrag-v3-chat   --desired-count 0
```

To resume, set the same values back to 1.

**🖱️ Console:**

- **EC2 → Auto Scaling groups → edgentrag-v3 → Edit** → desired 0 / min 0.
- **ECS → cluster → each service → Update service** → Desired tasks 0.

⚠️ The ALB, RDS, ElastiCache and WAF keep billing while paused. Delete them to stop the bill completely.
