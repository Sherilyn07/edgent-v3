# AWS Deployment — Console Only, us-east-1 (detailed)

> Deploying EdgentRAG v3 in **us-east-1 (N. Virginia)** from the **AWS
> Console**, click by click.
>
> **Every step has the same shape:**
>
> - 🟢 **What and why:** what this piece is, in plain words.
> - 🎯 **Decisions:** the choices we make, and what happens if you choose differently.
> - 🖱️ **Clicks:** exactly where to go, section by section, field by field.
> - ✅ **Check:** how to know it worked before moving on.
> - ❓ **If it goes wrong:** the common errors and what they mean.
>
> Replace `<ACCOUNT_ID>`, `<your-domain>` and `<your-github-repo>` with your own values.
> Console labels change now and then. If a field name differs slightly, look for the closest match.

---

## The map: what you'll build, in order

```
 STEP  WHAT                                   TIME (you + AWS waiting)
  0    find the default network (VPC)         2 min   — nothing created
  1    5 firewalls (security groups)          10 min
  2    Postgres database (RDS)                5 + 10 min waiting  ─┐ start both, keep going
  3    Redis (ElastiCache)                    5 + 10 min waiting  ─┘
  4    file storage (S3 bucket)               5 min
  5    8 queues (SQS)                         15 min
  6    secrets + parameters                   10 min
  7    sign-in (Cognito)                      10 min
  8    push code to GitHub (your PC)          5 min
  9    build + push 3 images (your PC)        20–40 min
 10    4 permission roles (IAM)               15 min
 11    ECS cluster + 3 log groups             5 min
 12    create the database tables (migrate)   5 min   ⚠️ gate: nothing runs before this
 13    2 worker services (Fargate)            15 min
 14    HTTPS certificate (ACM)                5 + up to 30 min waiting
 15    load balancer + API servers            20 + 15 min waiting
 16    firewall rules (WAF)                   10 min
 17    CloudFront (optional)                  15 min
 18    your domain name (DNS)                 5 min
 19    admin user + connect Colab + test      15 min
```

Total: about **4–5 hours** the first time.

---

## Before you start

### 1. Sign in with the right user

- **Don't use the root user** (the email you signed up with) for daily work.
- Use an **IAM user** or **IAM Identity Center** user with `AdministratorAccess`.
- ❓ Don't have one? **IAM → Users → Create user**:
  - ✅ Provide user access to the AWS Management Console
  - Attach policies directly → `AdministratorAccess`
  - Sign in with that user from now on.

### 2. Set the region to us-east-1, and keep checking it

- Top-right corner, next to your account name, there's a region dropdown.
- Choose **US East (N. Virginia) us-east-1**. The URL will then contain `region=us-east-1`.
- **Check it before every step.** The console remembers the last region **per service**, so opening a new service can quietly switch it back.
- If you create something in the wrong region, it's invisible from us-east-1 and nothing can connect to it.
- Some pages show **"Global"** instead of a region: IAM, CloudFront and Route 53. That's normal; they don't belong to a region.

**Why us-east-1:**

- It's the oldest and largest region, so every service and instance type is available there.
- **CloudFront certificates must live in us-east-1 anyway.** In this region, one certificate serves both the load balancer and CloudFront.

### 3. What the console can't do (you'll use these instead)

| Task | Tool | Step |
|---|---|---|
| Generate a password or token | **CloudShell**, the `>_` icon in the console's top bar. It opens a terminal in your browser, already logged in. | 2, 6 |
| Put the code on GitHub | **git** on your PC | 8 |
| Build + push Docker images | **Docker Desktop** on your PC | 9 |

### 4. x86_64, not ARM64

- The original scripts ran on a Mac and built ARM64 images.
- On a Windows PC, ARM64 needs slow emulation.
- So everything here is **x86_64**: images, task definitions and the EC2 AMI.

### 5. Check your account limits (new accounts are restricted)

- **Service Quotas** (search it) → **AWS services** → **Amazon Elastic Compute Cloud (Amazon EC2)** → search `Running On-Demand Standard`:
  - **Applied quota value** is in **vCPUs**.
  - **1** → you can run only one `t2.micro` or `t2.small`. It works, but it's slow.
  - Click the quota → **Request increase at account level** → 16. A human approves it, which can take hours, so do it now.
- **CloudFront** may later say *"Your account must be verified"*. The app works without it (step 17 is optional). If you want it: **Support → Create case → Account and billing**.

### 6. Set a budget alarm (recommended)

- **Billing and Cost Management → Budgets → Create budget → Use a template → Monthly cost budget**.
- Amount **$50**, with your email.
- You'll get an email if costs climb.

### Your value sheet

Keep this open in Notepad. Later steps ask for these values.

```
ACCOUNT_ID          = ____________        (top-right account menu, 12 digits)
VPC                 = vpc-__________
SUBNETS             = subnet-____ (1a)   subnet-____ (1b)   subnet-____ (1c)
SG ids              = alb sg-____  ec2 sg-____  ecs sg-____  rds sg-____  redis sg-____
DB password         = ________________________________ (from CloudShell, step 2)
RDS endpoint        = edgentrag-v3.xxxxxxxx.us-east-1.rds.amazonaws.com
Redis endpoint      = master.edgentrag-v3.xxxxxx.use1.cache.amazonaws.com
BUCKET              = edgentrag-v3-<ACCOUNT_ID>
Queue URLs          = https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/edgentrag-v3-ingest  (+ chat, stt, embed)
Broker token        = ________________________________ (step 6)
Secret ARNs         = database-url ____   redis-url ____   broker-token ____
Cognito             = pool us-east-1_______   client ______________
GitHub repo URL     = https://github.com/<you>/<repo>.git
ALB DNS             = edgentrag-v3-______.us-east-1.elb.amazonaws.com
```

---

## STEP 0: The network (VPC) — you do NOT create one

### 🟢 What is a VPC, in one breath

- A **VPC** (Virtual Private Cloud) is your private network inside AWS. Every server, database and container we create lives inside one.
- A VPC is split into **subnets**: smaller slices of the network, each in one **Availability Zone** (AZ), which is one physical data centre (`us-east-1a`, `us-east-1b`, …).
- **Every AWS account already has a "default VPC" in every region.** AWS made it for you, ready to use.

### 🎯 Our decision: use the default VPC

| | Default VPC (✅ what we use) | New VPC with private subnets |
|---|---|---|
| Setup | nothing, it's already there | ~15 settings + extra steps |
| Internet access | built in (Internet Gateway) | needs a **NAT gateway** |
| Cost | free | NAT ≈ **$32/month** + data, even when idle |
| Security | enforced by **security groups** (step 1); the database has no public IP | the same, plus an extra network wall |

**Why this is safe:** being in a "public" subnet does **not** make something reachable. Only a security group rule can let traffic in. Our database and Redis get **no public address**, and their firewalls only admit our own servers.

**What if you picked "private subnets" anyway:**

- The Fargate workers couldn't download their images or reach Colab without a NAT gateway.
- They would fail with `CannotPullContainerError`.

### 🖱️ Clicks

**1.** Top search bar → type **VPC** → open **VPC**.

**2.** Left menu → **Your VPCs**. You'll see a list like this:

```
Name   VPC ID                 State      IPv4 CIDR        Default VPC
-      vpc-0abc123def456      Available  172.31.0.0/16    Yes     ← this one
```

- ✍️ Copy the **VPC ID** into your value sheet.
- **Don't click "Create VPC".** That button opens the page with the two options, **"VPC only"** and **"VPC and more"**. We need neither. (What they mean is explained below, for your understanding.)
- ❓ **No row says "Default VPC: Yes"?** You (or someone) deleted it. Fix: **Actions → Create default VPC → Create**. That rebuilds it exactly as AWS made it, and it's free.

**3.** Left menu → **Subnets** → in the search box choose **VPC ID = the one you copied**. You'll see **6** subnets:

```
Name  Subnet ID              Availability Zone   IPv4 CIDR          Auto-assign public IPv4
-     subnet-0aaa...         us-east-1a          172.31.32.0/20     Yes   ✍️ copy
-     subnet-0bbb...         us-east-1b          172.31.0.0/20      Yes   ✍️ copy
-     subnet-0ccc...         us-east-1c          172.31.80.0/20     Yes   ✍️ copy
-     subnet-0ddd...         us-east-1d          ...                Yes
-     subnet-0eee...         us-east-1e          ...                Yes   ⚠️ avoid
-     subnet-0fff...         us-east-1f          ...                Yes
```

- ✍️ **Copy the 3 subnet ids for us-east-1a, 1b and 1c.**
- **Why 3, not 1?** The load balancer requires **at least 2** AZs. Postgres and Redis want a choice of AZs too. If one data centre has a problem, the others still work.
- **Why not us-east-1e?** Many newer instance types aren't sold there. If the Auto Scaling group picks 1e, the launch can fail.
- **"Auto-assign public IPv4 = Yes"** means servers started here get an internet address automatically. The EC2 boot script needs it to download Docker and your code from GitHub.

✅ **Check:** your value sheet has **1 VPC id + 3 subnet ids**. Nothing was created in this step.

### 📖 For understanding only: "VPC only" vs "VPC and more"

You don't need either. This is what they mean, if you're curious or want a separate network later:

- **VPC only:** creates *just the empty network*. No subnets, no internet gateway, no routes. You'd have to build all of those by hand afterwards, and nothing works until you do.
- **VPC and more:** a wizard that creates the VPC **plus** subnets, route tables, an internet gateway, and optionally NAT gateways and endpoints, all at once. It shows a preview diagram on the right.

If you ever want a dedicated VPC instead of the default one, these are the "VPC and more" settings that match this guide:

```
Resources to create:        VPC and more
Name tag auto-generation:   edgentrag-v3
IPv4 CIDR block:            10.0.0.0/16
IPv6 CIDR block:            No IPv6
Tenancy:                    Default
Number of AZs:              3
Public subnets:             3
Private subnets:            0          ← private subnets would force a NAT gateway ($)
NAT gateways:               None
VPC endpoints:              S3 Gateway (free; S3 traffic skips the internet)
DNS hostnames / resolution: ✅ ✅
```

- ⚠️ **One extra step afterwards:** the wizard's public subnets do **not** auto-assign public IPs.
  - For each subnet: **Subnets → select → Actions → Edit subnet settings → ✅ Enable auto-assign public IPv4 address**.
  - Without this, the EC2 server can't reach GitHub at boot.
- You'd then use **this** VPC and its subnets everywhere the guide says "default VPC".

---

## STEP 1: Security groups — the 5 firewalls

### 🟢 What and why

- A **security group** (SG) is a firewall attached to a resource. It says **who may connect in**, and on which port.
- We make **one per kind of thing**: the load balancer, the API servers, the workers, the database, and Redis.
- **The clever part:** a rule can name **another security group** as the source, instead of an IP address.
  - Example: "the database accepts connections from anything in the `edgentrag-v3-ec2` group".
  - When AWS starts a new server with a new IP, the rule still works. No updates needed.
- **The chain we're building:**

  ```
  Internet ──443/80──▶ [alb] ──8080──▶ [ec2] ──5432──▶ [rds]
                                         │   ──6379──▶ [redis]
                               [ecs] ────┘  (the same 5432 / 6379)
  ```

- **Why the ECS group gets no inbound rules:** the workers never *receive* connections. They only go *out* and ask the queue for work. With nothing allowed in, they're unreachable, and that's the safest possible setting.

### 🎯 Why create them empty first, then add rules

- The `rds` group needs a rule saying "allow the `ec2` group".
- But you can only pick a group that **already exists**.
- So: create all 5 empty (**1a**), then add the rules (**1b**).

### 🖱️ 1a. Create the 5 empty groups

**1.** Top search bar → **EC2** → left menu, scroll to **Network & Security → Security Groups** → orange **Create security group** button.

**2.** You'll see 4 sections. Fill them in like this for the **first** group:

```
── Basic details ────────────────────────────────────────────
Security group name:  edgentrag-v3-alb
Description:          Public entry point: the ALB      (required; plain words only)
VPC:                  pick the vpc-… you copied in step 0   ⚠️ check this every time

── Inbound rules ────────────────────────────────────────────
(leave empty — "This security group has no inbound rules")

── Outbound rules ───────────────────────────────────────────
(leave the one default rule: All traffic · All · 0.0.0.0/0)
   Why: our resources must reach out (to S3, SQS, Colab, GitHub).
   "Out" is safe; the danger is "in".

── Tags ─────────────────────────────────────────────────────
Add new tag → Key: Project   Value: edgentrag-v3
   Why: lets you find/delete everything for this project in one filter.
```

**3.** Click **Create security group**. Then go back to the list and repeat for the other 4:

| Security group name | Description |
|---|---|
| `edgentrag-v3-alb` | Public entry point: the ALB |
| `edgentrag-v3-ec2` | EC2 instances running web and api |
| `edgentrag-v3-ecs` | Fargate workers no inbound on purpose |
| `edgentrag-v3-rds` | RDS PostgreSQL |
| `edgentrag-v3-redis` | ElastiCache Redis |

✍️ Copy each **Security group ID** (`sg-…`) into your value sheet.

⚠️ **The most common mistake:** leaving **VPC** on a different VPC. Groups in different VPCs can't reference each other, and the RDS step later won't list the group.

### 🖱️ 1b. Add the inbound rules

For each group below:

- Click the group's **ID** in the list → the **Inbound rules** tab at the bottom → **Edit inbound rules** → **Add rule**.
- Each rule row has: **Type · Protocol · Port range · Source type · Source · Description**.
- Choosing **Type** fills in Protocol and Port for you.

**`edgentrag-v3-alb`** — the only group open to the internet:

```
Type    Port  Source type     Source       Description
HTTPS   443   Anywhere-IPv4   0.0.0.0/0    users over https
HTTP    80    Anywhere-IPv4   0.0.0.0/0    redirected to https by the ALB
```

- Why 80 too: someone typing `http://` should get redirected to https. Without this rule, their browser just **hangs**, with no error.

**`edgentrag-v3-ec2`** — only the load balancer may reach the API servers:

```
Type        Port  Source type  Source                          Description
Custom TCP  8080  Custom       click the box, type "alb" → pick edgentrag-v3-alb (sg-…)   from the ALB only
```

- 8080 is where nginx listens on the server.
- ❌ No SSH (22) rule. We use **Session Manager** for a shell, which needs no open port.

**`edgentrag-v3-rds`** — only our API servers and workers may reach Postgres:

```
Type        Port  Source type  Source                 Description
PostgreSQL  5432  Custom       edgentrag-v3-ec2       api servers
PostgreSQL  5432  Custom       edgentrag-v3-ecs       fargate workers
```

**`edgentrag-v3-redis`** — the same two groups:

```
Type        Port  Source type  Source                 Description
Custom TCP  6379  Custom       edgentrag-v3-ec2       api servers
Custom TCP  6379  Custom       edgentrag-v3-ecs       fargate workers
```

- Both sides need Redis: workers **publish** progress, and the API **listens** and relays it to the browser.
- Miss one, and live progress silently stops.

**`edgentrag-v3-ecs`** — don't open it, don't add anything.

Click **Save rules** after each group.

- ❓ **Your group isn't in the Source dropdown?** It's in a different VPC (see the warning in 1a). Delete it and recreate it in the right VPC.

✅ **Check.** On **Security Groups**, type `edgentrag-v3` in the search box. You should see 5 rows. Clicking each one → **Inbound rules** shows:

```
alb 2 rules · ec2 1 · rds 2 · redis 2 · ecs 0
```

---

## STEP 2: The database (RDS PostgreSQL) — start it now, it takes ~10 min

### 🟢 What and why

- **RDS** is a Postgres database that AWS runs for you. It handles backups, patching, and restarts if it crashes.
- It stores **everything permanent**: sessions, files, chunks, chat messages, **and the vectors** (thanks to the `pgvector` extension, added in step 12).
- **Without it:** there's nothing to save to, and every request fails.

### 🎯 Decisions

| Setting | Our choice | Why | If you choose differently |
|---|---|---|---|
| Engine | PostgreSQL 16 | pgvector needs ≥ 15 | Aurora: works, but costs more. MySQL: no pgvector at all. |
| Size | db.t4g.micro | cheapest; fine for a course | bigger = faster, and more $ |
| Availability | Single-AZ | half the price | Multi-AZ = survives a data-centre outage, 2× cost |
| Public access | **No** | only our servers may connect | Yes = exposed to the internet ❌ |
| Password | **Self managed** | the app reads one fixed URL | "Managed in Secrets Manager" **rotates** it, and the app breaks silently weeks later ❌ |
| Initial database name | `edgentrag` | the app connects to this name | left empty → the migration fails: *database "edgentrag" does not exist* |

### 🖱️ 2a. Make the password (CloudShell)

1. Click the **`>_`** icon in the top bar (CloudShell). Wait ~20 s for the terminal.
2. Paste and press Enter:

   ```bash
   python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))"
   ```

3. ✍️ Copy the 32-character result into your value sheet.
   - **Why letters and digits only:** the password goes inside a URL later. Symbols like `@ / : #` would break the URL.

### 🖱️ 2b. The subnet group

- **What it is:** a list telling RDS "you may live in these subnets".
- **Where:** search **RDS** → left menu **Subnet groups** → **Create DB subnet group**.

```
── Subnet group details ──
Name:          edgentrag-v3-db
Description:   edgentrag-v3 database subnets
VPC:           your default VPC (vpc-… from step 0)

── Add subnets ──
Availability Zones:  ☑ us-east-1a  ☑ us-east-1b  ☑ us-east-1c
Subnets:             ☑ the 3 subnet ids you copied in step 0
```

Click **Create**.

### 🖱️ 2c. The database

**RDS** → left menu **Databases** → **Create database**. Go through the page top to bottom:

```
── Choose a database creation method ──
◉ Standard create          (Easy create hides settings we need to change)

── Engine options ──
Engine type:     ◉ PostgreSQL          (NOT "Aurora (PostgreSQL Compatible)")
Engine version:  PostgreSQL 16.x  (pick the highest 16.x in the list)
                 Why 16: pgvector needs ≥ 15; 17 also works

── Templates ──
◉ Free tier   (if shown)  — otherwise ◉ Dev/Test

── Availability and durability ──
◉ Single-AZ DB instance deployment

── Settings ──
DB instance identifier:   edgentrag-v3
Master username:          edgentrag
Credentials management:   ◉ Self managed          ⚠️ NOT "Managed in AWS Secrets Manager"
Master password:          paste the password from 2a
Confirm master password:  paste again

── Instance configuration ──
◉ Burstable classes (includes t classes)
db.t4g.micro

── Storage ──
Storage type:        General Purpose SSD (gp3)
Allocated storage:   20 GiB
▸ Additional storage configuration → ☐ Enable storage autoscaling   (uncheck)
   Why: a runaway table should hit an error, not quietly grow your bill

── Connectivity ──
Compute resource:        ◉ Don't connect to an EC2 compute resource
Network type:            IPv4
VPC:                     your default VPC
DB subnet group:         edgentrag-v3-db
Public access:           ◉ No
VPC security group:      ◉ Choose existing
                         Existing: edgentrag-v3-rds   and click ✖ on "default" to remove it
Availability Zone:       No preference
▸ Additional configuration → Database port: 5432

── Database authentication ──
◉ Password authentication

── Monitoring ──
☐ Performance Insights (turn off; saves cost)
Enhanced monitoring: off

── Additional configuration (click to expand — IMPORTANT) ──
Initial database name:          edgentrag        ⚠️ the easiest thing to miss
DB parameter group:             default
Backup retention period:        1 day
☑ Encryption (default key)
Log exports:                    none
☐ Enable auto minor version upgrade    (no surprise restarts)
Deletion protection:            your choice (the teardown script turns it off)
```

Click **Create database**. If a pop-up offers "add-ons" or "connect to EC2", close it.

- **Don't wait here.** Go straight to step 3.

### ✅ Check (come back in ~10 min)

- **Databases** → `edgentrag-v3` → Status **Available**.
- Click it → **Connectivity & security** → ✍️ copy the **Endpoint**. It looks like `edgentrag-v3.abc123xyz.us-east-1.rds.amazonaws.com`.
- **Security** on the same tab should show `edgentrag-v3-rds` only.

### ❓ If it goes wrong

- **"Cannot find version 16"** → pick any version ≥ 15 that's offered.
- **The subnet group dropdown is empty** → the subnet group is in a different VPC from the one selected. Recreate it.
- **Forgot the initial database name** → easiest fix: delete the database (no snapshot) and recreate it.

---

## STEP 3: Redis (ElastiCache) — also ~10 min, start it now

### 🟢 What and why

- **Redis** is a very fast in-memory store. We use it for **4 short-lived things**:
  1. **Live progress (pub/sub):** a worker shouts "file done!", and the API server holding your browser connection hears it and passes it on.
  2. The last 6 chat turns, so follow-up questions make sense.
  3. The current Colab service addresses.
  4. 5-minute tickets for the live connection.
- **Without it:** no live progress, no chat memory, and the app can't find Colab.

### 🎯 Decisions

| Setting | Our choice | Why | If different |
|---|---|---|---|
| Deployment | **Design your own cache** | we need cluster mode OFF | **Serverless** is always clustered → live progress silently stops for some users ❌ |
| Cluster mode | **Disabled** | pub/sub doesn't reliably cross shards | Enabled → the same silent failure ❌ |
| Engine | Redis OSS 7.1 | what the app was tested with | Valkey is Redis-compatible and probably works, but it's untested here |
| Encryption in transit | **On** | traffic is encrypted | then the URL **must** start `rediss://` (2 s). Plain `redis://` just hangs |
| Replicas | 0 | cheapest | 1+ = survives a node failure, more $ |

### 🖱️ 3a. Subnet group

**ElastiCache** (search it) → left menu **Subnet groups** → **Create subnet group**

```
Name:          edgentrag-v3-cache
Description:   edgentrag-v3 redis subnets
VPC ID:        your default VPC
Selected subnets: Manage → keep only us-east-1a, 1b, 1c
```

Click **Create**.

### 🖱️ 3b. The cache

**ElastiCache** → left menu **Redis OSS caches** → **Create Redis OSS cache**

```
── Deployment option ──
◉ Design your own cache                 ⚠️ NOT "Serverless"

── Creation method ──
◉ Cluster cache   ·   ◉ Standard create   (not "Easy create", not "Restore from backup")

── Cluster mode ──
◉ Disabled                              ⚠️ the single most important setting here

── Cluster info ──
Name:          edgentrag-v3
Description:   pubsub, chat history, service urls, tickets

── Location ──
◉ AWS Cloud
☐ Multi-AZ      ☐ Auto-failover        (both off: 1 node only)

── Cache settings ──
Engine version:       7.1
Port:                 6379
Parameter group:      default.redis7
Node type:            cache.t4g.micro
Number of replicas:   0

── Connectivity ──
Network type:   IPv4
Subnet groups:  ◉ Choose existing subnet group → edgentrag-v3-cache
```

Click **Next**, then:

```
── Security ──
Encryption at rest:     ☑ Enable   · Encryption key: Default key
Encryption in transit:  ☑ Enable
Access control:         No access control
Selected security groups: Manage → ☑ edgentrag-v3-redis   ☐ default

── Backup ──
☐ Enable automatic backups

── Maintenance ──
Auto upgrade minor versions: ☐ off

── Tags ──
Project = edgentrag-v3
```

Click **Next** → review → **Create**.

### ✅ Check (~10 min)

- **Redis OSS caches** → `edgentrag-v3` → Status **Available**.
- Click it → **Cluster details**:
  - **Cluster mode = Disabled**
  - **Encryption in transit = Enabled**
  - ✍️ Copy the **Primary endpoint**, **without** `:6379` on the end. It looks like `master.edgentrag-v3.abcd12.use1.cache.amazonaws.com`.

### ❓ If it goes wrong

- **Cluster mode shows Enabled** → it can't be changed later. Delete the cache and recreate it.
- **You don't see "Design your own cache"** → you're on the Valkey or Memcached page. Go back to **Redis OSS caches**.

---

## STEP 4: File storage (S3 bucket)

### 🟢 What and why

- **S3** is AWS's file storage. The bucket holds:
  - uploaded files
  - extracted text
  - chunk files (for the GPU)
  - vector files (from the GPU)
- **The browser uploads straight to S3** using a signed, temporary link. File bytes **never pass through our API**, so a 2 GB video can't slow it down.
- **Without it:** there's nowhere to upload to.

### 🎯 Decisions

| Setting | Our choice | Why |
|---|---|---|
| Name | `edgentrag-v3-<ACCOUNT_ID>` | bucket names are **globally unique**, and your account id makes it yours |
| Block all public access | **On** | nobody can read files without a signed link. Signed links still work with this on. |
| CORS | **PUT from any origin** | ⚠️ without it, the browser refuses to upload, with the misleading error "upload failed — is the API running?" |
| Versioning | Off | files are written once, never edited |

### 🖱️ Clicks

**S3** → **Create bucket** (check the region at the top says **US East (N. Virginia)**):

```
── General configuration ──
AWS Region:     US East (N. Virginia) us-east-1
Bucket type:    ◉ General purpose
Bucket name:    edgentrag-v3-<ACCOUNT_ID>        e.g. edgentrag-v3-123456789012

── Object Ownership ──
◉ ACLs disabled (recommended)

── Block Public Access settings ──
☑ Block all public access                 (all 4 boxes ticked)

── Bucket Versioning ──
◉ Disable

── Tags ──
Project = edgentrag-v3

── Default encryption ──
◉ Server-side encryption with Amazon S3 managed keys (SSE-S3)
Bucket Key: ◉ Enable
```

Click **Create bucket**.

**Now the CORS rule:**

1. Click the bucket name → **Permissions** tab.
2. Scroll to the bottom → **Cross-origin resource sharing (CORS)** → **Edit**.
3. Delete anything in the box, paste this, then **Save changes**:

```json
[
  {
    "AllowedMethods": ["PUT"],
    "AllowedOrigins": ["*"],
    "AllowedHeaders": ["Content-Type"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

**What each line means:**

- `PUT`: the browser only ever uploads.
- `*`: any web page may *try*. That's fine, because only a valid signed link actually lets a file in.
- `Content-Type`: the one header the upload sends.
- `3000`: the browser remembers this permission for 50 min, instead of asking before every file.

### ✅ Check

- **Permissions** tab → **Block public access: On**, and the CORS box shows your rule.
- ✍️ The bucket name is in your value sheet.

---

## STEP 5: The queues (SQS) — 8 of them

### 🟢 What and why

- A **queue** is a to-do list. One program writes a note ("process file X"), another picks it up later.
- **Why:** the API never does slow work. It writes a note and answers you instantly.
  - If a worker crashes mid-job, the note **comes back** after a timeout and someone else does it.
  - Nothing is lost.
- **4 to-do lists:**

  | Queue | Holds | Who reads it |
  |---|---|---|
  | `ingest` | one note per uploaded file | the ingest worker |
  | `chat` | one note per question | the chat worker |
  | `stt` | "transcribe this video" | the Colab GPU (via the API broker) |
  | `embed` | "turn these chunks into vectors" | the Colab GPU (via the API broker) |

- **+4 "dead-letter queues" (DLQ):** a note that fails **5 times** is moved there instead of retrying forever. If a file is stuck, the DLQ is the first place to look.

### 🎯 Decisions — these numbers must match the code

| Setting | Value | Must match (`shared/config.py`) | If different |
|---|---|---|---|
| Visibility timeout | **15 minutes** | `queue_visibility_seconds=900` | shorter → a second worker grabs a job that's still running = duplicate work |
| Receive message wait time | **20 seconds** | `queue_wait_seconds=20` | 0 → thousands of empty requests (SQS bills per request) |
| Maximum receives | **5** | `queue_max_receives=5` | ≠ 5 → the app thinks "last try" at a different moment than SQS, and a file can hang at "processing" forever |
| Retention | 4 days | — | notes older than this vanish |

### 🖱️ Clicks — do this block 4 times (ingest, chat, stt, embed)

**Always create the DLQ first**, because the main queue must point at it.

**5a. The DLQ.** **SQS** (search it) → **Create queue**

```
Type:  ◉ Standard            (NOT FIFO)
Name:  edgentrag-v3-ingest-dlq
Everything else: leave default
```

Click **Create queue**.

**5b. The main queue.** Back to **Queues** → **Create queue**

```
── Details ──
Type:  ◉ Standard
Name:  edgentrag-v3-ingest

── Configuration ──
Visibility timeout:          15   Minutes
Message retention period:    4    Days
Delivery delay:              0    Seconds
Maximum message size:        256  KB  (default)
Receive message wait time:   20   Seconds

── Encryption ──
Server-side encryption: Enabled · SSE-SQS (default)

── Access policy ──
◉ Basic · leave defaults (only your account)

── Dead-letter queue ──
◉ Enabled
Choose queue:      edgentrag-v3-ingest-dlq
Maximum receives:  5

── Tags ──
Project = edgentrag-v3
```

Click **Create queue**. On the page that opens, ✍️ copy the **URL**:
`https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/edgentrag-v3-ingest`

**Repeat 5a + 5b** with `chat`, `stt` and `embed` in place of `ingest`.

### ✅ Check

- **Queues** list, search `edgentrag-v3`: **8 rows**, all Type **Standard**.
- Click `edgentrag-v3-chat` → **Dead-letter queue** tab → it names `edgentrag-v3-chat-dlq`, max receives 5.
- ✍️ 4 main queue URLs in your value sheet.

### ❓ If it goes wrong

- **You created a FIFO queue by mistake** (its name ends in `.fifo`) → the type can't be changed. Delete it and recreate it as Standard.
- **"Queue already exists" after deleting one** → SQS blocks reusing a name for **60 seconds**. Wait a minute.

---

## STEP 6: Secrets and settings

### 🟢 What and why

Two different stores, for two kinds of values:

| Store | For | Why |
|---|---|---|
| **Secrets Manager** | passwords and tokens (database URL, Redis URL, broker token) | encrypted; the servers fetch them at start, so they never appear in files or the console |
| **Parameter Store** | non-secret settings (bucket name, queue URLs, Cognito ids) | free; one place both the EC2 servers and the workers read from |

- **The broker token:** the Colab GPU's only password. It allows exactly one thing: "give me a job".

### 🖱️ 6a. Generate the broker token (CloudShell)

```bash
python3 -c "import secrets;print(secrets.token_hex(32))"
```

✍️ Copy it into your value sheet.

### 🖱️ 6b. Three secrets

**Secrets Manager** → **Store a new secret**. Do this 3 times:

```
── Step 1: Choose secret type ──
◉ Other type of secret
Key/value pairs: click the "Plaintext" tab → DELETE everything in the box ({"":""}) → paste ONLY the value
Encryption key:  aws/secretsmanager

── Step 2: Configure secret ──
Secret name:   (see table)
Description:   (optional)
Tags:          Project = edgentrag-v3

── Step 3: Configure rotation ──
☐ Automatic rotation   (leave off)

── Step 4: Review → Store
```

| Secret name | The value to paste (Plaintext) |
|---|---|
| `edgentrag-v3/database-url` | `postgresql://edgentrag:<DB password>@<RDS endpoint>:5432/edgentrag` |
| `edgentrag-v3/redis-url` | `rediss://<Redis primary endpoint>:6379/0` |
| `edgentrag-v3/broker-token` | the token from 6a |

**The easy mistakes:**

- `postgresql://`, **not** `postgres://`.
- `rediss://`, with **two s's**, because encryption in transit is on.
- **Plaintext tab**, not key/value. The app expects the bare URL, not `{"key":"url"}`.
- No spaces or line breaks at the ends.

After storing each one, click it → ✍️ copy the **Secret ARN**. It ends with a random suffix like `-AbC123`, which is normal.

### 🖱️ 6c. Six parameters

**Systems Manager** (search "Parameter Store") → **Parameter Store** → **Create parameter**. Do this 6 times:

```
Name:        (see table)
Tier:        ◉ Standard
Type:        ◉ String
Data type:   text
Value:       (see table)
```

| Name | Value |
|---|---|
| `/edgentrag-v3/s3-bucket` | `edgentrag-v3-<ACCOUNT_ID>` |
| `/edgentrag-v3/aws-region` | `us-east-1` |
| `/edgentrag-v3/ingest-queue-url` | the ingest queue URL |
| `/edgentrag-v3/chat-queue-url` | the chat queue URL |
| `/edgentrag-v3/stt-queue-url` | the stt queue URL |
| `/edgentrag-v3/embed-queue-url` | the embed queue URL |

(3 more are added in step 7.)

### ✅ Check

- **Secrets Manager** → 3 secrets named `edgentrag-v3/…`.
- **Parameter Store** → search `/edgentrag-v3` → 6 parameters.
- To double-check a secret: open it → **Retrieve secret value**. It should show **one line**, the bare URL.

---

## STEP 7: Sign-in (Cognito) — before building anything

### 🟢 What and why

- **Cognito** runs the whole login system: sign-up, email codes, passwords, "forgot password", and **tokens**.
  - A token is a signed pass the browser shows the API on every request.
- **Why:** every session gets an **owner**. You only ever see your own documents and chats.
- **Why now, before building:** the website **bakes the Cognito ids into its JavaScript when it's built** (step 8 → 15). Create the pool first and you build once. Do it later and you rebuild.

### 🎯 Decisions

| Setting | Our choice | Why | If different |
|---|---|---|---|
| App type | **Single-page application** | a browser app can't keep a secret, so SPA creates a client **without** one | "Traditional web app" → a client **with** a secret, and sign-in fails in the browser ❌ |
| Sign-in with | Email | the frontend sends the email as the username | username sign-in → the frontend doesn't match |
| Self sign-up | On | users create their own accounts | off → only admins can create users |
| Auth flows | SRP + refresh token | SRP never sends the password itself | "USER_PASSWORD_AUTH" sends the raw password; not needed |
| Group `admins` | exact name | the API checks for it to allow changing the Colab URLs | a different name → you can never set the URLs ❌ |

### 🖱️ 7a. Create the pool

**Cognito** → **User pools** → **Create user pool**. The wizard is a single page, **"Set up resources for your application"**:

```
Define your application:
  Application type:            ◉ Single-page application (SPA)
  Name your application:       edgentrag-v3-web

Configure options:
  Options for sign-in identifiers:  ☑ Email     (☐ Phone  ☐ Username)
  Self-registration:                ☑ Enable self-registration
  Required attributes for sign-up:  email      (already required)

Add a return URL (optional):   leave EMPTY
  Why: our app has its own login screen (Login.jsx). It doesn't use Cognito's hosted page.
```

Click **Create user directory**. The next page shows sample code. Ignore it and click **Go to overview**.

- The pool gets an auto-generated name like `User pool - abc123`. That's fine.
- The teardown script finds the pool by the id you store in Parameter Store, not by its name.

### 🖱️ 7b. Password rules

Left menu → **Authentication → Authentication methods** (or **Sign-in**) → **Password policy** → **Edit**:

```
◉ Custom
Password minimum length:  8
☑ Contains at least 1 number
☑ Contains at least 1 lowercase letter
☑ Contains at least 1 uppercase letter
☐ Contains at least 1 special character     (relaxed for a course project)
Temporary passwords expire in: 7 days
```

**Save changes.**

### 🖱️ 7c. The app client

Left menu → **Applications → App clients** → click `edgentrag-v3-web`:

- ✍️ Copy the **Client ID**.
- **Client secret** must be empty or say "no client secret".
  - ❓ If there's a secret, you picked the wrong app type. Go back to **App clients** → **Create app client** → type **Single-page application** → name `edgentrag-v3-web-spa`, and use that one's Client ID instead.

Click **Edit** (top-right of the app client page):

```
Authentication flows:
  ☑ ALLOW_USER_SRP_AUTH            ← how the login screen signs in
  ☑ ALLOW_REFRESH_TOKEN_AUTH       ← how you stay signed in
  ☐ ALLOW_USER_AUTH  ☐ ALLOW_USER_PASSWORD_AUTH  ☐ ALLOW_CUSTOM_AUTH  ☐ ALLOW_ADMIN_USER_PASSWORD_AUTH
Refresh token expiration:  30 days
Access token expiration:   60 minutes
ID token expiration:       60 minutes
☑ Enable token revocation
Prevent user existence errors: ◉ Enabled
   Why: login errors never reveal whether an email has an account
```

**Save changes.**

### 🖱️ 7d. The admins group

Left menu → **User management → Groups** → **Create group**:

```
Group name:  admins          (lower case, exactly)
Description: may change the Colab service addresses
IAM role / Precedence: leave empty
```

**Create group.** It's empty for now. You'll add yourself in step 19.

### 🖱️ 7e. Save the ids

- **Overview** (top of the left menu) → ✍️ copy the **User pool ID**, e.g. `us-east-1_AbCdEf123`.
- **Parameter Store → Create parameter** × 3 (Standard · String):

  | Name | Value |
  |---|---|
  | `/edgentrag-v3/cognito-region` | `us-east-1` |
  | `/edgentrag-v3/cognito-user-pool-id` | `us-east-1_…` |
  | `/edgentrag-v3/cognito-app-client-id` | the Client ID |

### ✅ Check

- Paste this into a browser tab, with your pool id:

  ```
  https://cognito-idp.us-east-1.amazonaws.com/<POOL_ID>/.well-known/jwks.json
  ```

  - You should see JSON with a `"keys"` list of **2** entries, `"alg":"RS256"`. The API uses these to check every login token.
  - A **404** or an error means the pool id or region is wrong.

---

## STEP 8: Put the code on GitHub (on your PC)

### 🟢 What and why

- Each EC2 server **downloads your code from GitHub when it starts** (`git clone`), then builds the website and API itself.
- So GitHub must have your code, **with your Cognito ids in `frontend/.env.production`**.

### 🎯 Decisions

| Choice | Why |
|---|---|
| **Public** repo | the server can clone it without a password. A private repo would need a deploy key and extra setup. |
| Cognito ids in the repo | safe: they only *identify* your pool, and grant nothing |
| `.env` **never** in the repo | it would contain real passwords. It's already in `.gitignore`. |

### 🖱️ Steps on your PC

1. **Edit `frontend/.env.production`** so it says:

   ```ini
   VITE_API_BASE=/api
   VITE_COGNITO_REGION=us-east-1
   VITE_COGNITO_USER_POOL_ID=us-east-1_AbCdEf123
   VITE_COGNITO_APP_CLIENT_ID=<your client id>
   ```

2. **Create an empty repo on GitHub:** github.com → **+** (top-right) → **New repository**:

   ```
   Owner:            you
   Repository name:  edgentrag-v3
   Visibility:       ◉ Public
   ☐ Add a README   ☐ Add .gitignore   ☐ Choose a license     ← leave ALL unticked
   ```

   **Create repository.**
   - **Why empty:** this folder already has its own history. Any file GitHub creates would make the first push fail with *"rejected: fetch first"*.

3. **Push this folder to it.** Open **Git Bash** in the v3 folder:

   ```bash
   # a. Stop Windows from marking the scripts as changed.
   #    Checking out on Windows made git think their "executable" bit was removed.
   git config core.fileMode false

   # b. Check what will be committed. There must be NO line ending in ".env"
   #    (frontend/.env.production is fine and needed).
   git status

   # c. Commit everything: your Cognito ids, the guides, the teardown script.
   git add -A
   git commit -m "Deploy to us-east-1: Cognito ids, guides, teardown script"

   # d. Point at YOUR repo and push.
   #    "origin" currently points to the original author's repo (you can't push there).
   #    Rename it to "upstream" (you can still pull his updates from it),
   #    and make YOUR repo "origin".
   git remote rename origin upstream
   git remote add origin https://github.com/<you>/edgentrag-v3.git
   git remote -v                     # origin = yours, upstream = abhaykes1's
   git push -u origin main
   ```

   - The first push opens a **browser window to sign in to GitHub** (Git Credential Manager). Approve it.
   - Later pushes are just `git push`, because `-u` made `origin` the default.
   - **The commit history comes along.** His 2 commits keep his name, and yours show your name. That's the honest record of who wrote what.
   - To pull his later updates: `git pull upstream main`.

4. ✍️ Put the **clone URL** in your value sheet, e.g. `https://github.com/<you>/edgentrag-v3.git`. The boot script in step 15c uses it.

### ❓ If it goes wrong

| What you see | Fix |
|---|---|
| `remote mine already exists` | you ran it twice. Harmless; carry on with the push. |
| `rejected … fetch first` | the GitHub repo isn't empty. Delete it and recreate it with nothing ticked, **or** run `git pull mine main --allow-unrelated-histories` first. |
| `Permission denied` / 403 | you're signed in to a different GitHub account. Windows: **Credential Manager → Windows Credentials** → remove `git:https://github.com` → push again. |
| `src refspec main does not match any` | your branch has another name. `git branch` shows it; push that name instead. |

### ✅ Check

- Open the repo on github.com.
- `docker-compose.ec2.yml` is at the **top level** (not inside a subfolder).
- `frontend/.env.production` shows **your** ids.
- There is **no** `.env` file.

---

## STEP 9: Build and upload the 3 program images (Docker Desktop, on your PC)

### 🟢 What and why

- AWS doesn't run source code; it runs **images**: sealed boxes with the code plus everything it needs.
- **3 images, from 3 Dockerfiles:**
  - `api`: used for the one-off migration (step 12)
  - `ingest-worker`: ~3.5 GB, includes Docling for PDFs
  - `chat-worker`: ~120 MB
- **ECR** is AWS's private image store. The workers download their images from there.
- (The EC2 servers don't use ECR. They build `web` + `api` themselves from GitHub.)

### 🎯 Decisions

| Choice | Why | If different |
|---|---|---|
| `--platform linux/amd64` | matches your PC and the task definitions (X86_64) | an ARM image on an X86 task → `CannotPullContainerError … platform` ❌ |
| Build from the **repo root** with `-f` | each image copies the shared `shared/` folder | build inside `workers/ingest/` → `COPY shared` fails ❌ |

### 🖱️ 9a. Create 3 repositories

**ECR** (search "Elastic Container Registry") → **Private registry → Repositories** → **Create repository**, 3 times:

```
Repository name:        edgentrag-v3/api          (then edgentrag-v3/ingest-worker, then edgentrag-v3/chat-worker)
Image tag mutability:   Mutable     (so :latest can be overwritten on redeploy)
Encryption:             AES-256
Image scan settings:    ☑ Scan on push   (free check for known vulnerabilities)
```

### 🖱️ 9b. Build + push (PowerShell on your PC)

**Prepare:**

- Start **Docker Desktop** and wait for "Engine running".
- Free disk: **15 GB+**.
- The AWS CLI must be installed and configured (`aws configure` with an access key for your user), because the login command below uses it.

**Log in:** open any repo → **View push commands** → **Windows** tab → copy and run **only command 1** (the login). It ends with `Login Succeeded`.

**From the v3 folder** (the repo root):

```powershell
$REG = "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com"

docker build --platform linux/amd64 -f backend/Dockerfile -t "$REG/edgentrag-v3/api:latest" .
docker push "$REG/edgentrag-v3/api:latest"

docker build --platform linux/amd64 -f workers/chat/Dockerfile -t "$REG/edgentrag-v3/chat-worker:latest" .
docker push "$REG/edgentrag-v3/chat-worker:latest"

docker build --platform linux/amd64 -f workers/ingest/Dockerfile -t "$REG/edgentrag-v3/ingest-worker:latest" .
docker push "$REG/edgentrag-v3/ingest-worker:latest"
```

- The **last `.`** on each build line matters: it means "use this folder as the starting point".
- The ingest build takes the longest (10–30 min).

### ✅ Check (in the console, not the terminal)

- **ECR → Repositories** → each of the 3 repos → an image tagged **`latest`**, with a size (api ~0.5 GB, chat ~0.1 GB, ingest ~3.5 GB).
- **Why check here:** a push can fail while the terminal still looks fine. ECR is the truth.

### ❓ If it goes wrong

- **`no basic auth credentials` / `401`** → the login expired (it lasts 12 h). Run command 1 again.
- **`input/output error` or the build dies** → the disk is full. Run `docker system prune -a` and free up space.
- **`COPY shared … not found`** → you ran the build from the wrong folder. `cd` to the repo root.

---

## STEP 10: Permissions (IAM roles) — 4 of them

### 🟢 What and why

- In AWS, a program can do **nothing** unless a **role** allows it.
- A role is a permission slip. We give each program **only** what it needs:
  - A chat worker that's somehow hacked still can't read your files, because its role has no S3 access.
- **The 4 roles:**

| Role | Used by | May do |
|---|---|---|
| `edgentrag-v3-execution-role` | **ECS itself**, before your code starts | pull images from ECR, read the 2 URL secrets, write logs |
| `edgentrag-v3-ingest-task-role` | **your ingest worker code** | read the ingest queue · send to the stt/embed queues · read/write the bucket |
| `edgentrag-v3-chat-task-role` | **your chat worker code** | read the chat queue. **Nothing else.** |
| `edgentrag-v3-ec2` | **the API servers** | queues, bucket, 3 secrets (incl. broker token), parameters, Session Manager |

### 🎯 Execution role vs task role — the one to understand

- **Execution role:** ECS uses it to **start** the container.
  - If it's wrong, the task never starts: *"unable to pull secrets / image"*.
- **Task role:** **your code** uses it while running.
  - If it's wrong, the task starts, then fails with *AccessDenied* on its first queue call.
- **Neither role mentions Postgres or Redis.** Those use a **password** plus the **security groups** from step 1, not IAM.

### 🖱️ 10a. The three ECS roles

**IAM** → **Roles** → **Create role**:

```
── Step 1: Select trusted entity ──
Trusted entity type:  ◉ AWS service
Service or use case:  Elastic Container Service
Use case:             ◉ Elastic Container Service Task
   (this lets ECS tasks "wear" the role)

── Step 2: Add permissions ──
ingest-task-role / chat-task-role:  select NOTHING → Next
execution-role:                     search and ☑ AmazonECSTaskExecutionRolePolicy → Next

── Step 3: Name, review, create ──
Role name: edgentrag-v3-ingest-task-role   (then chat-task-role, then execution-role)
Tags: Project = edgentrag-v3
```

**Create role.** Do this 3 times.

**Now the extra permissions (inline policies).** For each role:

- **IAM → Roles** → click the role → **Add permissions** (dropdown) → **Create inline policy** → **JSON** tab.
- Delete the sample, paste the JSON below (replace `<ACCOUNT_ID>`), then **Next** → Policy name → **Create policy**.

**`edgentrag-v3-ingest-task-role`**, policy name `ingest-worker`:

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow",
  "Action":["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:ChangeMessageVisibility","sqs:GetQueueAttributes"],
  "Resource":"arn:aws:sqs:us-east-1:<ACCOUNT_ID>:edgentrag-v3-ingest"},
 {"Effect":"Allow","Action":"sqs:SendMessage",
  "Resource":["arn:aws:sqs:us-east-1:<ACCOUNT_ID>:edgentrag-v3-stt",
              "arn:aws:sqs:us-east-1:<ACCOUNT_ID>:edgentrag-v3-embed"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],
  "Resource":"arn:aws:s3:::edgentrag-v3-<ACCOUNT_ID>/*"}]}
```

What it allows:

- **Read and delete** from the ingest queue, which is its own to-do list.
- **Send** to the stt and embed queues, to ask the GPU for work.
- **Read and write files** in the bucket.

**`edgentrag-v3-chat-task-role`**, policy name `chat-worker`:

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow",
  "Action":["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:ChangeMessageVisibility","sqs:GetQueueAttributes"],
  "Resource":"arn:aws:sqs:us-east-1:<ACCOUNT_ID>:edgentrag-v3-chat"}]}
```

- Only its own queue. It talks to Postgres (by password) and to Colab (over the internet), neither of which needs IAM.

**`edgentrag-v3-execution-role`**, policy name `read-deployment-secrets`. Use the **full ARNs** from your value sheet:

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"secretsmanager:GetSecretValue",
  "Resource":["<DATABASE_URL_SECRET_ARN>","<REDIS_URL_SECRET_ARN>"]}]}
```

- It doesn't include the broker token: no worker uses it. Only the API needs it (step 10b).

### 🖱️ 10b. The EC2 role

**IAM → Roles → Create role**:

```
Trusted entity type:  ◉ AWS service
Use case:             ◉ EC2
Permissions:          ☑ AmazonSSMManagedInstanceCore
   Why: lets you open a shell on the server from the console (Session Manager),
        with no SSH port and no key file
Role name:            edgentrag-v3-ec2
```

**Create role.** The console also creates an **instance profile** with the same name. An instance profile is the "holder" that attaches a role to a server; you'll pick it in step 15.

Then **Add permissions → Create inline policy → JSON**, policy name `api-runtime`:

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow",
  "Action":["sqs:SendMessage","sqs:GetQueueAttributes","sqs:ReceiveMessage","sqs:DeleteMessage","sqs:ChangeMessageVisibility"],
  "Resource":"arn:aws:sqs:us-east-1:<ACCOUNT_ID>:edgentrag-v3-*"},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],
  "Resource":"arn:aws:s3:::edgentrag-v3-<ACCOUNT_ID>/*"},
 {"Effect":"Allow","Action":"secretsmanager:GetSecretValue",
  "Resource":["<DATABASE_URL_SECRET_ARN>","<REDIS_URL_SECRET_ARN>","<BROKER_TOKEN_SECRET_ARN>"]},
 {"Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],
  "Resource":"arn:aws:ssm:us-east-1:<ACCOUNT_ID>:parameter/edgentrag-v3/*"}]}
```

What the API needs:

- **Send** to queues. It also **receives** from stt/embed on behalf of the GPU (the broker).
- **Sign** upload and download links for the bucket.
- **Read the 3 secrets and the parameters** when the server boots.

### ✅ Check

- **IAM → Roles**, search `edgentrag-v3`: **4 roles**.
- Open each one → **Permissions** tab:
  - execution: 1 managed + 1 inline
  - ingest: 1 inline
  - chat: 1 inline
  - ec2: 1 managed + 1 inline

### ❓ If it goes wrong

- **The JSON editor shows a red error** → a missing quote or comma. The placeholders `<…>` must be replaced **inside** the quotes.
- **The wrong ARN only shows up later** (step 13), as `AccessDenied … on resource arn:aws:sqs:…`. Compare the ARN in the error with the one in your policy, character by character.

---

## STEP 11: ECS cluster + log groups

### 🟢 What and why

- **ECS** runs containers. A **cluster** is just a named group for them. On **Fargate** there are no servers to manage: you say "run this image with 1 CPU", and AWS finds a machine.
- **Log groups** in CloudWatch are where each container's printed output goes. That's your only window into a running worker.

### 🖱️ 11a. The cluster

**ECS** (search "Elastic Container Service") → **Clusters** → **Create cluster**:

```
Cluster name:     edgentrag-v3
Infrastructure:   ☑ AWS Fargate (serverless)      ☐ Amazon EC2 instances
Monitoring:       ◉ Container Insights (standard)  — or "with enhanced observability"
   Why: gives task-count metrics; needed if you add autoscaling later
Tags:             Project = edgentrag-v3
```

**Create.**

- ❓ **"Unable to assume the service linked role"** → the first-ever use of ECS in the account creates a helper role, and it can lag a bit. Wait 30 s and click Create again.

### 🖱️ 11b. Three log groups

**CloudWatch** → left menu **Logs → Log groups** → **Create log group**, 3 times:

```
Log group name:     /ecs/edgentrag-v3-migrate      (then -ingest, then -chat)
Retention setting:  1 week    (logs are kept, and billed, forever otherwise)
Log class:          Standard
```

- **Why create them first:** a task that crashes in its first second still has somewhere to write *why*.

### ✅ Check

- **ECS → Clusters** → `edgentrag-v3`, Status Active.
- **CloudWatch → Log groups**, search `edgentrag` → 3 groups.

---

## STEP 12: Create the database tables (the migration) — ⚠️ gate

### 🟢 What and why

- The empty database needs **tables** (sessions, files, chunks, messages) and the **pgvector** extension, plus a vector column and index.
- A one-off program, `python -m scripts.migrate`, creates them. It's already inside the `api` image.
- **We run it once, as its own task**, not every time a program starts.
  - With many servers starting at the same moment, they'd all try to create the same tables at once and trip over each other.
- **Why it's a gate:** if a worker starts before the tables exist, it crashes. If the API starts first, every request fails.

### 🎯 Why the task must get a public IP

- Our subnets have no NAT gateway (step 0).
- A task needs **some** way out to download its image from ECR.
- With **Public IP on**, it goes out directly. The `ecs` security group still lets **nothing in**.

### 🖱️ 12a. The task definition

**ECS** → **Task definitions** → **Create new task definition** (dropdown) → **Create new task definition with JSON**.

Delete the sample, paste this (replace **4** placeholders), then **Create**:

```json
{
  "family": "edgentrag-v3-migrate",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/edgentrag-v3-execution-role",
  "containerDefinitions": [{
    "name": "api",
    "image": "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/api:latest",
    "essential": true,
    "command": ["python", "-m", "scripts.migrate"],
    "secrets": [{"name": "DATABASE_URL", "valueFrom": "<DATABASE_URL_SECRET_ARN>"}],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/edgentrag-v3-migrate",
      "awslogs-region": "us-east-1",
      "awslogs-stream-prefix": "migrate"}}
  }]
}
```

**What the less obvious lines mean:**

- `X86_64` must match how you built the image in step 9.
- `command` replaces the image's normal start command (the web server) with the migration.
- `secrets` makes ECS fetch the database URL from Secrets Manager and hand it to the program as `DATABASE_URL`. The password is never written into this definition.

### 🖱️ 12b. Run it once

**ECS → Clusters → edgentrag-v3** → **Tasks** tab → **Run new task**:

```
── Compute configuration ──
Compute options:   ◉ Launch type
Launch type:       FARGATE
Platform version:  LATEST

── Deployment configuration ──
Application type:  ◉ Task
Family:            edgentrag-v3-migrate     Revision: LATEST
Desired tasks:     1

── Networking ──
VPC:               your default VPC
Subnets:           keep us-east-1a, 1b, 1c (remove the others)
Security group:    ◉ Use an existing security group → edgentrag-v3-ecs   (remove "default")
Public IP:         ◉ Turned on        ⚠️ required
```

Click **Create**.

### ✅ Check

- Click the new task and wait ~1–2 min. Status goes PROVISIONING → PENDING → RUNNING → **STOPPED**.
- **Stopped is correct:** the job finished.
- **Logs** tab should show:

  ```
  INFO    applying migrations up to head
  INFO    Running upgrade  -> 0001_initial_schema
  INFO    Running upgrade 0001_initial_schema -> 0002_pgvector
  INFO    done
  ```

- On the task's details, **Exit code: 0**.

### ❓ If it goes wrong

| What you see | Meaning | Fix |
|---|---|---|
| `DATABASE_URL is sqlite; nothing to migrate` | the secret didn't reach the program | check the secret ARN in the task definition; check the execution role's inline policy |
| `CannotPullContainerError` | couldn't download the image | Public IP was off, **or** the image was built for ARM |
| `ResourceInitializationError: unable to pull secrets` | the execution role can't read the secret | fix the ARNs in `read-deployment-secrets` |
| `could not connect to server` / timeout | network blocked | the `rds` security group must allow **`edgentrag-v3-ecs`** on 5432 |
| `password authentication failed` | the password in the URL is wrong | fix the `database-url` secret |
| `database "edgentrag" does not exist` | the initial database name was missing in step 2 | recreate the DB with the name set |

Fix, then **Run new task** again. Running it twice is harmless: it only applies what's missing.

---

## STEP 13: The two worker services

### 🟢 What and why

- A **service** keeps a program running **forever**. If the program crashes, ECS starts a new copy.
- Two services, each running 1 copy:

| Service | Does | Size |
|---|---|---|
| `edgentrag-v3-ingest` | reads uploaded files, chunks them, saves vectors | 1 vCPU, 2 GB RAM, 30 GB disk (PDF tools + videos) |
| `edgentrag-v3-chat` | answers questions | 0.5 vCPU, 1 GB RAM |

- **No load balancer and no ports:** workers never receive connections. They ask the queue for work.
- 💰 **Cost:** these run 24/7 (~$30–40/month for both) until you pause them.

### 🖱️ 13a. Two task definitions

**ECS → Task definitions → Create new task definition with JSON**. Paste each one, replacing the placeholders, then **Create**.

**Ingest:**

```json
{
  "family": "edgentrag-v3-ingest",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "1024",
  "memory": "2048",
  "ephemeralStorage": {"sizeInGiB": 30},
  "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/edgentrag-v3-execution-role",
  "taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/edgentrag-v3-ingest-task-role",
  "containerDefinitions": [{
    "name": "ingest-worker",
    "image": "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/ingest-worker:latest",
    "essential": true,
    "stopTimeout": 120,
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "<DATABASE_URL_SECRET_ARN>"},
      {"name": "REDIS_URL", "valueFrom": "<REDIS_URL_SECRET_ARN>"}
    ],
    "environment": [
      {"name": "ENV", "value": "aws"},
      {"name": "AWS_REGION", "value": "us-east-1"},
      {"name": "S3_BUCKET", "value": "edgentrag-v3-<ACCOUNT_ID>"},
      {"name": "INGEST_QUEUE_URL", "value": "https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/edgentrag-v3-ingest"},
      {"name": "STT_QUEUE_URL", "value": "https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/edgentrag-v3-stt"},
      {"name": "EMBED_QUEUE_URL", "value": "https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/edgentrag-v3-embed"}
    ],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/edgentrag-v3-ingest", "awslogs-region": "us-east-1", "awslogs-stream-prefix": "ingest"}}
  }]
}
```

**Chat:**

```json
{
  "family": "edgentrag-v3-chat",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "512",
  "memory": "1024",
  "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/edgentrag-v3-execution-role",
  "taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/edgentrag-v3-chat-task-role",
  "containerDefinitions": [{
    "name": "chat-worker",
    "image": "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/edgentrag-v3/chat-worker:latest",
    "essential": true,
    "stopTimeout": 60,
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "<DATABASE_URL_SECRET_ARN>"},
      {"name": "REDIS_URL", "valueFrom": "<REDIS_URL_SECRET_ARN>"}
    ],
    "environment": [
      {"name": "ENV", "value": "aws"},
      {"name": "AWS_REGION", "value": "us-east-1"},
      {"name": "CHAT_QUEUE_URL", "value": "https://sqs.us-east-1.amazonaws.com/<ACCOUNT_ID>/edgentrag-v3-chat"}
    ],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/edgentrag-v3-chat", "awslogs-region": "us-east-1", "awslogs-stream-prefix": "chat"}}
  }]
}
```

**What the less obvious lines mean:**

- `stopTimeout` is how long a worker may finish its current job when AWS stops it, e.g. during a redeploy.
- `environment` holds the non-secret settings. `secrets` holds the ones fetched from Secrets Manager.
- **The chat worker has no Colab addresses here.** It reads them from Redis at runtime, because they change every time Colab restarts.

### 🖱️ 13b. Two services

**ECS → Clusters → edgentrag-v3** → **Services** tab → **Create**. Do this twice:

```
── Environment ──
Compute options:  ◉ Launch type → FARGATE · Platform LATEST

── Deployment configuration ──
Application type:  ◉ Service
Task definition:   Family edgentrag-v3-ingest   (2nd time: edgentrag-v3-chat)  · Revision LATEST
Service name:      edgentrag-v3-ingest           (2nd time: edgentrag-v3-chat)
Service type:      Replica
Desired tasks:     1
Deployment options: Rolling update (default: min 100% / max 200%)
   Why: during an update, the new copy starts before the old one stops, so nothing is missed

── Networking ──
VPC:              default
Subnets:          us-east-1a, 1b, 1c
Security group:   ◉ existing → edgentrag-v3-ecs only
Public IP:        ◉ Turned on        (same reason as step 12; also needed to reach Colab)

── Load balancing ──
None                                  (workers take no incoming connections)

── Service auto scaling ──
leave off for now (see 13c)

── Tags ──
Project = edgentrag-v3
```

Click **Create**.

### ✅ Check (~2 min)

- **Services** tab → each service shows **1/1 Tasks running**, and its **Deployments** tab shows *Completed*.
- Click a task → **Logs** tab should show exactly this, then **nothing more**:

  ```
  shared.db      database_url is not sqlite; schema is managed by Alembic
  shared.worker  ingest: consuming edgentrag-v3-ingest
  ```

  **Silence after that line is healthy.** The worker is waiting, asking the queue every 20 s.

### ❓ If it goes wrong

| What you see | Meaning |
|---|---|
| tasks start and stop over and over | open a **stopped** task → **Stopped reason**, and its Logs |
| `AccessDenied … sqs:receivemessage` | a typo in the task role's queue ARN (step 10) |
| `no queue url configured` | an environment variable name or value is wrong in the task definition |
| `Timeout connecting to Redis` | the `redis` security group doesn't allow `edgentrag-v3-ecs`, or the URL is `redis://` instead of `rediss://` |

### 13c. Autoscaling — optional; skip it for now

- The original rule ("add a worker when there are more than 2 queued messages per worker") needs **metric math**, which the ECS console can't build.
- **Recommendation:** keep 1 task each. That's plenty while you learn.
- **If you want scaling later (ingest only):**
  1. **CloudWatch → Alarms → Create alarm → Select metric → SQS → Queue Metrics** → `edgentrag-v3-ingest` / `ApproximateNumberOfMessagesVisible` → Average, 1 minute → Greater than **2** → remove the notification → name `ingest-backlog-high`.
  2. Make a second alarm: Lower than **1** → `ingest-backlog-low`.
  3. **ECS → service edgentrag-v3-ingest → Update service → Service auto scaling** → min 1, max 3:
     - **Step scaling** policy: `ingest-backlog-high` → **Add 1** task.
     - A second step policy: `ingest-backlog-low` → **Remove 1** task.

---

## STEP 14: The HTTPS certificate

### 🟢 What and why

- To serve `https://your-domain`, the load balancer needs a **certificate** proving you own the domain. **ACM** gives them out for free.
- To prove ownership, ACM asks you to add one DNS record.
- **us-east-1 bonus:** CloudFront **only** accepts certificates from us-east-1. Because everything here is in us-east-1, **one certificate serves both** the load balancer and CloudFront.

### 🎯 Decision: which name

- Use a **subdomain**, e.g. `study.example.com`, not the bare `example.com`.
- **Why:** a load balancer has no fixed IP, only a name. DNS allows pointing a **subdomain** at a name (a CNAME), but not the bare domain.
- The exception is Route 53, whose "alias" records work on the bare domain too.

### 🖱️ Clicks

**Certificate Manager (ACM)**, region **us-east-1** → **Request a certificate**:

```
◉ Request a public certificate → Next
Fully qualified domain name:   study.example.com       (your subdomain)
Allow export:                  Disable export
Validation method:             ◉ DNS validation
Key algorithm:                 RSA 2048
Tags:                          Project = edgentrag-v3
```

Click **Request**. Open the certificate: Status **Pending validation**. In **Domains**, you'll see:

```
CNAME name:   _a1b2c3d4e5.study.example.com.
CNAME value:  _f6g7h8i9.xyzabc.acm-validations.aws.
```

**Add that record at your DNS provider:**

- **Route 53 (domain managed in AWS):** click **Create records in Route 53** → **Create records**. Done.
- **Anyone else** (GoDaddy, Namecheap, Google Cloud DNS, Cloudflare…): add a new record:
  - Type **CNAME**
  - Name = the CNAME name. Many providers want **only the part before your domain**, e.g. `_a1b2c3d4e5.study`.
  - Value = the CNAME value
  - TTL 300
  - ⚠️ **Cloudflare:** set the proxy to **DNS only** (grey cloud).

### ✅ Check

- Status becomes **Issued** (usually 5–30 min). Refresh the page.
- ❓ **Still pending after an hour?** The record name is usually doubled, e.g. `…study.example.com.example.com`. Look it up at `https://dnschecker.org`: search the CNAME name, type CNAME.

---

## STEP 15: The load balancer and the API servers

### 🟢 What and why

This is the public front door:

```
browser ──https──▶ ALB (load balancer) ──8080──▶ EC2 server(s): nginx (website) + FastAPI (API)
```

- **Target group:** the list of servers the ALB may send traffic to, and how to test if each is healthy.
- **ALB:** handles HTTPS, redirects http → https, and spreads traffic across servers.
- **Launch template:** the "recipe" for a server: which OS, what size, which role, and a **boot script**.
- **Auto Scaling group (ASG):** keeps N servers running from that recipe, and replaces any that fail health checks.

### 🎯 Decisions

| Setting | Our choice | Why | If different |
|---|---|---|---|
| Health check path | **`/api/health`** | checks only "is the app process alive" | `/api/ready` also checks the database, so one DB hiccup marks **every** server unhealthy and the whole site goes down ❌ |
| ALB idle timeout | **300 s** | live progress uses one long-open connection | default 60 s → the ALB silently cuts it every minute |
| IMDS hop limit | **2** | the API runs **inside Docker**, one network "hop" further from the server's credentials | 1 → `NoCredentialsError` inside the container, while everything looks fine on the server |
| Health check grace | **600 s** | the first boot builds the website and API (slow) | 300 → servers get killed mid-build, forever |
| Instance type | t3.medium (2 vCPU, 4 GB) — or **t2.small** if your quota is 1 vCPU | builds + runs 2 containers | t2.micro (1 GB) → out of memory |
| Server count | min 2 if the quota allows, else 1 | 2 = a deploy or crash never takes the site down | 1 = brief downtime on replacement |

### 🖱️ 15a. Target group

**EC2** → left menu **Load Balancing → Target Groups** → **Create target group**:

```
── Basic configuration ──
Target type:         ◉ Instances
Target group name:   edgentrag-v3-api
Protocol : Port:     HTTP : 8080
IP address type:     IPv4
VPC:                 default
Protocol version:    HTTP1

── Health checks ──
Health check protocol:  HTTP
Health check path:      /api/health              ⚠️
▸ Advanced health check settings:
   Port:                 Traffic port
   Healthy threshold:    2
   Unhealthy threshold:  3
   Timeout:              5
   Interval:             30
   Success codes:        200
```

**Next** → **Register targets**: leave empty (the ASG adds servers itself) → **Create target group**.

### 🖱️ 15b. Load balancer

**EC2 → Load Balancers** → **Create load balancer** → **Application Load Balancer → Create**:

```
── Basic configuration ──
Load balancer name:  edgentrag-v3
Scheme:              ◉ Internet-facing
IP address type:     ◉ IPv4

── Network mapping ──
VPC:        default
Mappings:   ☑ us-east-1a (its subnet)  ☑ us-east-1b  ☑ us-east-1c

── Security groups ──
edgentrag-v3-alb only   (✖ remove "default")

── Listeners and routing ──
Listener 1:  Protocol HTTPS · Port 443 · Default action: Forward to → edgentrag-v3-api

── Secure listener settings ──
Security policy:          ELBSecurityPolicy-TLS13-1-2-2021-06    (modern TLS only)
Default SSL/TLS certificate: ◉ From ACM → study.example.com

── Tags ──
Project = edgentrag-v3
```

**Create load balancer.** It takes ~3 min to become **Active**. Then click it:

1. **Listeners and rules** tab → **Add listener**:

   ```
   Protocol: HTTP · Port: 80
   Default action: ◉ Redirect to URL
     ◉ URI parts · Protocol HTTPS · Port 443 · Status code 301 - permanently moved
   ```

   **Add.** Now `http://` visitors get redirected to https.

2. **Attributes** tab → **Edit**:

   ```
   Connection idle timeout:  300 seconds          ⚠️
   ```

   **Save changes.**

3. ✍️ Copy the **DNS name** from the **Details** section, e.g. `edgentrag-v3-1234567890.us-east-1.elb.amazonaws.com`.

### 🖱️ 15c. The boot script (user data)

- This runs **once**, the first time each server starts.
- Put your GitHub URL on the `git clone` line.
- Keep it in Notepad; you'll paste it in 15d.

```bash
#!/bin/bash
exec > >(tee /var/log/edgentrag-bootstrap.log) 2>&1
set -euxo pipefail
REGION=us-east-1
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
curl -fsSL https://get.docker.com | sh
snap install aws-cli --classic
mkdir -p /opt/edgentrag && cd /opt/edgentrag
git clone --depth 1 <your-github-repo> app && cd app
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

**What it does, line by line:**

1. Everything it prints is saved to `/var/log/edgentrag-bootstrap.log`, for debugging.
2. Stop at the first error.
3. Add **4 GB of swap**, extra "memory" on disk, so building the website on a 2 GB server doesn't crash.
4. Install **Docker** and the **AWS CLI**.
5. Download your code from GitHub.
6. Build the `.env` file from Parameter Store and Secrets Manager, using the server's role. No passwords live in the script.
7. Build and start the two containers, `web` (nginx + website) and `api`.

### 🖱️ 15d. Launch template

**EC2 → Instances → Launch Templates** → **Create launch template**:

```
── Launch template name and description ──
Name:         edgentrag-v3
Description:  web + api servers
☑ Provide guidance to help me set up a template that I can use with EC2 Auto Scaling

── Application and OS Images (AMI) ──
Quick Start → Ubuntu → Ubuntu Server 24.04 LTS (HVM), SSD Volume Type · Architecture 64-bit (x86)

── Instance type ──
t3.medium    (quota ≥ 2 vCPU)   —   or   t2.small   (quota = 1 vCPU)

── Key pair ──
Don't include in launch template   (we use Session Manager, not SSH)

── Network settings ──
Subnet:           Don't include in launch template   (the ASG chooses)
Firewall:         ◉ Select existing security group → edgentrag-v3-ec2

── Configure storage ──
1 volume: 30 GiB · gp3
   Why: the Docker images + build cache need ~10–15 GB

── Resource tags ──
Name = edgentrag-v3          Resource types: Instances
Project = edgentrag-v3       Resource types: Instances

── Advanced details (expand) ──
IAM instance profile:          edgentrag-v3-ec2
Metadata accessible:           Enabled
Metadata version:              V2 only (token required)
Metadata response hop limit:   2                        ⚠️
User data:                     paste the whole script from 15c
```

Click **Create launch template**.

### 🖱️ 15e. Auto Scaling group

**EC2 → Auto Scaling → Auto Scaling Groups** → **Create Auto Scaling group**:

```
── Step 1: Choose launch template ──
Name:             edgentrag-v3
Launch template:  edgentrag-v3 · Version: Latest

── Step 2: Instance launch options ──
VPC:                     default
Availability Zones and subnets:  us-east-1a, us-east-1b, us-east-1c

── Step 3: Integrate with other services ──
Load balancing:          ◉ Attach to an existing load balancer
                         ◉ Choose from your load balancer target groups → edgentrag-v3-api
VPC Lattice:             No
Health checks:           ☑ Turn on Elastic Load Balancing health checks
Health check grace period: 600 seconds                  ⚠️
   Why ELB checks: EC2's own check only asks "is the machine on?" — it stays
   "healthy" even when the app inside is dead. ELB's check calls /api/health.

── Step 4: Group size and scaling ──
Desired capacity:  1   (or 2 with enough quota)
Min desired:       1   (or 2)
Max desired:       1   (or 4)
   Why max = 1 on a 1-vCPU quota: otherwise it keeps trying, and failing, to launch a 2nd server
Automatic scaling: ◉ No scaling policies
Instance maintenance policy: No policy

── Step 5: Notifications ── skip
── Step 6: Tags ── Project = edgentrag-v3
── Step 7: Review → Create Auto Scaling group
```

### ✅ Check (wait 10–15 min; the first boot builds everything)

1. **EC2 → Instances**: one `edgentrag-v3` instance, Running.
2. **Target Groups → edgentrag-v3-api → Targets** tab → Health status goes `initial` → **healthy**.
3. In a browser: `https://<ALB DNS name>/api/health`.
   - The browser warns about the certificate. That's expected: the certificate is for your domain, not the ALB's name.
   - Continue anyway, and you should see `{"status":"ok","service":"api","env":"aws"}`.

### ❓ If it goes wrong

| What you see | Where to look | Usual cause |
|---|---|---|
| No instance appears at all | **ASG → Activity** tab | vCPU quota (see "Before you start") — the error shows **only** here |
| Instance up, target **unhealthy** | open a shell (below) and read the boot log | the build is still running; or `git clone` failed (private repo?); or out of memory (exit code 137) |
| Instance keeps being replaced | ASG → Activity | grace period too short: set it to 600 |
| `/api/health` OK but the app says auth errors | Parameter Store Cognito values | typo in a pool or client id |

**Open a shell on the server** (no SSH needed): **EC2 → Instances** → select it → **Connect** → **Session Manager** tab → **Connect**. Then:

```bash
sudo tail -60 /var/log/edgentrag-bootstrap.log      # what the boot script did
sudo docker ps                                      # expect: edgentrag-v3-web-1 and edgentrag-v3-api-1, "Up (healthy)"
curl -s localhost:8080/api/health                   # {"status":"ok",...}
```

---

## STEP 16: WAF — the web firewall

### 🟢 What and why

- **WAF** inspects every web request **before** it reaches the ALB, and blocks known attacks (SQL injection, bad bots, known-malicious IPs) and floods.
- Security groups only filter by **port**. WAF looks **inside** the request.

### 🎯 Decision: where to put it

- **No CloudFront** (the normal case) → attach WAF to the **ALB** (this step).
- **With CloudFront** (step 17) → attach WAF to **CloudFront** instead, and skip this step.
  - A WAF attached to CloudFront has scope **CloudFront**. One attached to an ALB has scope **Regional**.
  - They're separate objects: you can't convert one into the other.

### 🖱️ Clicks

**WAF & Shield** → left menu **Web ACLs** → region dropdown **US East (N. Virginia)** → **Create web ACL**:

```
── Step 1: Describe web ACL and associate it to AWS resources ──
Resource type:   ◉ Regional resources
Region:          US East (N. Virginia)
Name:            edgentrag-v3-alb
CloudWatch metric name: edgentrag-v3-alb
Associated AWS resources: Add AWS resources → ◉ Application Load Balancer → ☑ edgentrag-v3 → Add

── Step 2: Add rules and rule groups ──
Add rules → Add managed rule groups → expand "AWS managed rule groups" → toggle ON:
   ☑ Core rule set                  (common web attacks)
   ☑ Known bad inputs               (exploit patterns)
   ☑ Amazon IP reputation list      (IPs known for abuse)
   → Add rules
Add rules → Add my own rules and rule groups → Rule builder:
   Name:                 RateLimitPerIP
   Type:                 ◉ Rate-based rule
   Rate limit:           2000
   Evaluation window:    5 minutes
   Request aggregation:  ◉ Source IP address
   Scope:                ◉ Consider all requests
   Action:               ◉ Block
   → Add rule
Default web ACL action for requests that don't match any rules:  ◉ Allow

── Step 3: Set rule priority ──
order: Core → Known bad inputs → IP reputation → RateLimitPerIP

── Step 4: Configure metrics ── defaults
── Step 5: Review → Create web ACL
```

- **Why 2000 per 5 minutes:** a real person never gets close. A runaway script does.

### ✅ Check

- **Web ACLs** → `edgentrag-v3-alb` → **Associated AWS resources** tab lists the ALB.
- `https://<ALB DNS name>/api/health` still works.

### ❓ If it goes wrong

- **`WAFUnavailableEntityException`** → the ALB is still starting. Wait a minute and try again.
- **Real users get 403s** → **Web ACL → Sampled requests** shows which rule blocked them.

---

## STEP 17 (optional): CloudFront

### 🟢 What and why

- **CloudFront** is AWS's global network of edge servers. Users connect to the nearest one, which caches the website files and forwards API calls to your ALB.
- **You can skip it:** the app works fine on the ALB alone.
- It needs a **verified account**. A new account may get *"Your account must be verified"*, and only AWS Support can lift that.

### 🎯 The one setting that breaks everything if wrong

- By default, CloudFront **strips** most headers and query strings before forwarding.
- Our API needs:
  - the `Authorization` header (your login)
  - `?ticket=` (the live-progress connection)
- The origin request policy **AllViewer** forwards them. Without it, every call fails with 401/403.

### 🖱️ Clicks

**CloudFront** → **Create distribution**:

```
Origin domain:                  pick the ALB (edgentrag-v3-….elb.amazonaws.com)
Protocol:                       ◉ HTTPS only · Minimum origin SSL protocol TLSv1.2
Origin response timeout:        60 seconds
Default cache behavior:
   Viewer protocol policy:      ◉ Redirect HTTP to HTTPS
   Allowed HTTP methods:        ◉ GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE
   Cache policy:                CachingOptimized     (website files)
Web Application Firewall:       ◉ Enable security protections
Alternate domain name (CNAME):  study.example.com
Custom SSL certificate:         the ACM certificate from step 14
```

**Create distribution.** Then open it → **Behaviors** tab → **Create behavior**, twice. **Add the events one first**, so it sits higher in the list:

```
Path pattern:              /api/sessions/*/events
Origin:                    the ALB
Viewer protocol:           Redirect HTTP to HTTPS
Allowed methods:           GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE
Cache policy:              CachingDisabled
Origin request policy:     AllViewer              ⚠️
Compress objects:          No

Path pattern:              /api/*
(same as above, Compress: Yes)
```

**Then lock the ALB** so nobody can bypass CloudFront and its firewall:

- **Security Groups → edgentrag-v3-alb → Edit inbound rules** → change the **443** rule's Source to:
  - **Prefix list → `com.amazonaws.global.cloudfront.origin-facing`**
- Delete the 80 rule (CloudFront handles redirects now).

---

## STEP 18: Your domain name (DNS)

### 🟢 What and why

- DNS turns `study.example.com` into "go to this load balancer".
- **Why a CNAME:** the ALB's IP addresses change, but its **name** doesn't. So you point **your name** at **its name**.

### 🖱️ Clicks — at your DNS provider

| You use… | Add this record |
|---|---|
| Just the ALB (normal) | Type **CNAME** · Name `study` · Value `<ALB DNS name>` · TTL 300 |
| CloudFront | Type **CNAME** · Name `study` · Value `dxxxxxxxx.cloudfront.net` · TTL 300 |
| **Route 53** | **Route 53 → Hosted zones → your domain → Create record** → Name `study` · Type **A** · ☑ **Alias** → Route traffic to **Alias to Application Load Balancer** (or CloudFront) · **US East (N. Virginia)** · pick it → Create |

- **TTL 300 = 5 minutes:** if you point it somewhere else later, the change spreads quickly.
- ⚠️ **Cloudflare:** set the record to **DNS only** (grey cloud), or Cloudflare adds its own proxy in front.

### ✅ Check (DNS can take 5–30 min)

1. `https://study.example.com/api/health` → `{"status":"ok","service":"api","env":"aws"}`, with **no** certificate warning.
2. `http://study.example.com/` → redirects to `https://`.
3. `https://study.example.com/` → the app's sign-in screen.
4. **The header test**, in CloudShell:

   ```bash
   curl -s -H "Authorization: Bearer nonsense" https://study.example.com/api/config/services
   ```

   - `{"detail":"unreadable token"}` → ✅ the login header reached the API.
   - `{"detail":"missing bearer token"}` → ❌ something in front stripped it (CloudFront AllViewer).

---

## STEP 19: Make yourself admin, connect the Colab GPU, test

### 🟢 What and why

- The three AI models (embedding, speech-to-text, LLM) run on a **Colab GPU**, not in AWS.
- Colab connects in **two directions**:
  1. **Colab → us:** it asks our API "any work?" for embedding and transcription jobs, using the **broker token**.
  2. **Us → Colab:** the chat worker calls Colab directly for answers, using the 3 URLs you paste into the app.
- Only **admins** may set those URLs, so first make yourself one.

### 🖱️ Clicks

1. **Create your account:** open `https://study.example.com` → **Sign up** → email + password (8+ characters, upper, lower, number) → enter the **code from your email** → sign in.
2. **Make yourself admin:** **Cognito → User pools** → your pool → **User management → Users** → click your email → **Add user to group** → ☑ `admins` → **Add**.
3. **Sign out of the app and sign back in.**
   - ⚠️ This is always forgotten. Your admin status lives **inside your login token**, and the old token was made before you joined the group.
4. **Get the broker token:** **Secrets Manager → edgentrag-v3/broker-token → Retrieve secret value** → copy.
5. **On Colab**, in `services/.env`:

   ```ini
   BROKER_URL=https://study.example.com/api
   BROKER_TOKEN=<the value you copied>
   ```

   Then run `bash run_colab.sh`. At the end it prints 3 `https://….trycloudflare.com` URLs.
6. **In the app's first screen**, paste the 3 URLs (embedding, stt, llm) → Save.
   - The app checks that each one answers **and** is the right service.
7. **Test:**
   - Upload a small `.txt` **and** a short video, since they take different paths.
   - Watch the progress update live, until the session says **ready**.
   - Ask a question. The answer should arrive with its sources listed.

### ✅ Where to look when something is stuck

| Symptom | Look at |
|---|---|
| Upload fails instantly | S3 CORS (step 4) |
| File stuck on "processing" | CloudWatch → `/ecs/edgentrag-v3-ingest` · then SQS → `edgentrag-v3-embed-dlq` → **Messages available** > 0 means the GPU failed 5 times |
| Progress never updates live (only after refresh) | Redis cluster mode / Serverless (step 3) · ALB idle timeout (step 15b) |
| Answer never comes | CloudWatch → `/ecs/edgentrag-v3-chat` · are the Colab URLs still alive? |
| "Save" on the Colab URLs → 403 | you're not in `admins`, or didn't sign out and back in (step 19.3) |
| Everything 401 | the Cognito ids in `frontend/.env.production` don't match the pool (step 8) |

---

## Pausing to save money

| Resource | How to pause | Still billing? |
|---|---|---|
| API servers | **EC2 → Auto Scaling groups → edgentrag-v3 → Edit** → Desired 0, Min 0 | no |
| Workers | **ECS → cluster → each service → Update service** → Desired tasks 0 | no |
| Database | **RDS → edgentrag-v3 → Actions → Stop temporarily** | storage only; ⚠️ it auto-starts after 7 days |
| ALB, ElastiCache, WAF | can't be paused | **yes** — delete to stop |

To resume, set the counts back to 1 and start RDS.

---

## Tearing it all down (CLI)

You built everything in the console, but you can delete it with one script. It finds each resource by the name this guide used.

```bash
aws configure                      # once: access key of your IAM user, region us-east-1
DOMAIN=study.example.com bash scripts/teardown_us_east_1.sh
```

**How it runs:**

- It asks you to type `delete`, then removes everything in the right order.
- It waits for RDS and ElastiCache to finish deleting (~10 min each).
- Anything already gone shows as `skip`, so re-running is safe.

**If a security group shows "still in use":** AWS is still releasing network connections. Wait 5 minutes and run it again.

**Do these by hand afterwards:**

- The DNS records at your provider (the app record + the ACM validation record).
- CloudFront, if you made one: **Disable** → wait until *Deployed* → **Delete**.
