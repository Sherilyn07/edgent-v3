#!/usr/bin/env bash
#
# Task 3 of the EdgentRAG v3 deployment — the S3 bucket.
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# WHY A NEW BUCKET
# ----------------
# DEPLOY.md says to reuse v1/v2's bucket (<your-s3-bucket>) and not
# recreate it. That bucket lives in a DIFFERENT AWS account from this one, so
# there is nothing to reuse here. This is a fresh bucket in the same region as
# everything else — which also avoids cross-region transfer charges on every
# upload and every model-service fetch.
#
# WHAT THE BUCKET HOLDS — four kinds of object, see shared/storage.py:
#   raw/…          the uploaded file, PUT by the browser
#   text/…         extracted plain text, written by the ingest worker
#   chunks/…       chunks.jsonl, read by the GPU
#   vectors/…      computed vectors, written by the GPU, read back by ingest
#
# THE THING WORTH TEACHING HERE
# -----------------------------
# File bytes never pass through the API. The browser PUTs straight to S3 with
# a presigned URL, and the Colab GPU reads and writes with presigned URLs too.
# The API's only involvement is signing — pure computation, milliseconds. That
# is what lets a 2 GB video upload run against an API that never handles more
# than a few KB of it.
#
# A presigned URL is an ordinary https address with a signature attached that
# says "whoever holds this may do this one operation on this one object until
# this time." It is why the GPU needs no AWS credentials at all.
#
# HOW TO RUN
#   bash scripts/create_s3_bucket.sh

set -euo pipefail

export AWS_REGION=ap-southeast-1

# Your AWS account id, looked up rather than hardcoded — so this script works
# in whatever account your credentials point at, not just the one it was
# written in.
ACC=$(aws sts get-caller-identity --query Account --output text)

# Bucket names are globally unique across every AWS account on earth, so they
# need a disambiguator. Appending the account id is the standard trick: always
# available to you, and it documents ownership.
BUCKET=edgentrag-v3-${ACC}

# ---------------------------------------------------------------------------
# Step 1 — create the bucket
# ---------------------------------------------------------------------------
# --create-bucket-configuration LocationConstraint is required in EVERY region
# except us-east-1, where passing it is an ERROR. That asymmetry is a classic
# stumble when a script written against us-east-1 is run anywhere else, or the
# reverse. us-east-1 was the original region and predates the parameter.
aws s3api create-bucket --region "$AWS_REGION" --bucket "$BUCKET" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"

# ---------------------------------------------------------------------------
# Step 2 — block public access, explicitly
# ---------------------------------------------------------------------------
# All four of these default to true on new buckets nowadays. Set them anyway:
# it is one command, it documents the intent, and it does not depend on what
# the default happened to be the day the bucket was made.
#
# This does NOT interfere with presigned URLs. A presigned URL is an
# authenticated request that carries a signature derived from the signer's own
# IAM credentials — it is not public access, and these settings do not touch
# it. Students reliably expect a conflict here; there isn't one.
aws s3api put-public-access-block --region "$AWS_REGION" --bucket "$BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# ---------------------------------------------------------------------------
# Step 3 — default encryption at rest
# ---------------------------------------------------------------------------
# SSE-S3 (AES256) with S3-managed keys: free, transparent, nothing to
# configure on the client. SSE-KMS would give per-key audit trails and access
# control, and would also add a KMS charge per request plus a KMS grant on
# every role that touches the bucket. Not worth it here.
#
# BucketKeyEnabled reduces KMS request volume; harmless with SSE-S3 and
# correct to leave on if you later switch to KMS.
aws s3api put-bucket-encryption --region "$AWS_REGION" --bucket "$BUCKET" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

aws s3api put-bucket-tagging --region "$AWS_REGION" --bucket "$BUCKET" \
  --tagging 'TagSet=[{Key=Project,Value=edgentrag-v3}]'

# ---------------------------------------------------------------------------
# Step 4 — CORS. NOT OPTIONAL, AND NOT IN DEPLOY.md
# ---------------------------------------------------------------------------
# This step is missing from the runbook, and without it EVERY UPLOAD FAILS.
# It is the single most valuable thing in this file.
#
# Why it is needed:
#   frontend/src/api.js::uploadToStorage does
#       xhr.open('PUT', url)
#       xhr.setRequestHeader('Content-Type', file.type)
#   The page is served from the CloudFront domain; the PUT goes to
#   s3.ap-southeast-1.amazonaws.com. Different origin. And because it sets a
#   Content-Type header on a PUT, the browser first sends a preflight OPTIONS
#   request. S3 answers that preflight from the bucket's CORS configuration
#   alone. No configuration, no permissive answer, and the browser refuses to
#   send the actual PUT.
#
# Why it is so hard to diagnose:
#   The failure arrives at xhr.onerror, whose message is
#       "upload failed — is the API running?"
#   The API is running perfectly. The request never reached the API, and was
#   never going to — it was aimed at S3. Every instinct points at the wrong
#   component. Hours disappear here.
#
# The fields:
#   AllowedMethods  PUT only. The browser never GETs, DELETEs or lists.
#   AllowedHeaders  Content-Type — the one header uploadToStorage sets. This
#                   list is what the preflight asks permission for.
#   ExposeHeaders   ETag, so JS could read it if upload logic ever needs it.
#   MaxAgeSeconds   how long the browser may cache the preflight answer, so
#                   uploading fifty files sends one OPTIONS, not fifty.
#
# AllowedOrigins is "*" for now because CloudFront does not exist yet. TIGHTEN
# IT to the real domain once DEPLOY.md section 10 is done. Worth being precise
# with students about what that is and is not: CORS is a browser-side policy,
# not an access control. "*" does not let anyone write to the bucket — only a
# valid presigned URL does that, and those are minted by the API for an
# authenticated, session-owning user. Tightening it is good hygiene that
# limits which pages may *attempt* the request; it is not the security
# boundary. The presigned URL is.
cat > ./cors.json <<'EOF'
{
  "CORSRules": [
    {
      "AllowedMethods": ["PUT"],
      "AllowedOrigins": ["*"],
      "AllowedHeaders": ["Content-Type"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }
  ]
}
EOF

aws s3api put-bucket-cors --region "$AWS_REGION" --bucket "$BUCKET" \
  --cors-configuration file://./cors.json

rm -f ./cors.json

# ---------------------------------------------------------------------------
# Step 5 — verify
# ---------------------------------------------------------------------------
aws s3api get-bucket-cors --region "$AWS_REGION" --bucket "$BUCKET" --output json

# All four must be true.
aws s3api get-public-access-block --region "$AWS_REGION" --bucket "$BUCKET" \
  --query PublicAccessBlockConfiguration --output json

# Must equal $AWS_REGION. A bucket in the wrong region still works but adds a
# cross-region charge to every single object read and write.
aws s3api get-bucket-location --region "$AWS_REGION" --bucket "$BUCKET" --output text

# ---------------------------------------------------------------------------
# Step 6 — record it where the deployment reads it from
# ---------------------------------------------------------------------------
# SSM Parameter Store, not Secrets Manager: a bucket name is not a secret, and
# Parameter Store's String type is free. Both the EC2 launch template's user
# data and the ECS task definitions read this one value, which is the point of
# centralising it — the alternative is the same string typed in two places,
# drifting the first time one is edited.
aws ssm put-parameter --region "$AWS_REGION" \
  --name /edgentrag-v3/s3-bucket --type String --value "$BUCKET" --overwrite

aws ssm put-parameter --region "$AWS_REGION" \
  --name /edgentrag-v3/aws-region --type String --value "$AWS_REGION" --overwrite

aws ssm get-parameters --region "$AWS_REGION" \
  --names /edgentrag-v3/s3-bucket /edgentrag-v3/aws-region \
  --query 'Parameters[].{Name:Name,Value:Value}' --output table

# ---------------------------------------------------------------------------
# NOT DONE HERE, deliberately
# ---------------------------------------------------------------------------
# No lifecycle rule. A real deployment should expire raw/ and chunks/ objects
# after N days — chunks.jsonl in particular is scratch data the GPU reads once
# and never needs again. Left out because "storage grows forever" is a lesson
# better learned from a bill than from a rule that silently prevented it.
#
# No versioning. It would double storage for a workload where every object is
# written once and never modified.
#
# No bucket policy. The two IAM task roles (DEPLOY.md 6.3) and the EC2
# instance role (7.1) grant s3:GetObject/PutObject on this bucket's ARN from
# the *identity* side, which is sufficient and keeps permissions in one place.
# A bucket policy would be a second place to look when something is denied.

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
#   aws s3 rm s3://$BUCKET --recursive        # a bucket must be EMPTY to delete
#   aws s3api delete-bucket --region $AWS_REGION --bucket $BUCKET
#   aws ssm delete-parameters --region $AWS_REGION --names /edgentrag-v3/s3-bucket /edgentrag-v3/aws-region
#
# ---------------------------------------------------------------------------
# NEXT: task 4 — `python -m scripts.bootstrap` creates the eight SQS queues
# (four work, four dead-letter) and prints a BROKER_TOKEN. Needs the .env from
# .env.example with S3_BUCKET set to the bucket above.
