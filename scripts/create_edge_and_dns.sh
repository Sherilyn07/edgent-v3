#!/usr/bin/env bash
#
# Task 9 of the EdgentRAG v3 deployment — TLS certificates, WAF, CloudFront,
# and DNS. The last step: this is what makes the app reachable at a real name.
#
# Every command below was executed successfully in ap-southeast-1 / us-east-1
# on 2026-09-12 — EXCEPT the CloudFront distribution, which was blocked by an
# AWS account restriction. That block, and the fallback we shipped instead, is
# documented in step 4 rather than hidden.
#
# THE END STATE THIS BUILDS TOWARDS
#
#     browser -> CloudFront (+WAF) -> ALB -> EC2 (nginx -> api)
#
# and the one we could actually reach today:
#
#     browser -> ALB (+WAF) -> EC2 (nginx -> api)
#
# The difference is caching, global PoPs, and where WAF sits. Everything else —
# TLS, the hostname, auth, the API — is identical, which is why the fallback is
# a real deployment and not a toy.
#
# DNS NOTE: this domain's nameservers are GOOGLE CLOUD DNS, not Route 53.
# Nothing in AWS requires Route 53. ACM validates by CNAME, and a CNAME is a
# CNAME wherever it lives. DEPLOY.md says Route 53 only because that is what
# its author used.
#
# HOW TO RUN
#   bash scripts/create_edge_and_dns.sh

set -euo pipefail

export AWS_REGION=ap-southeast-1
DOMAIN=study.edgent.in
GCP_ZONE=edgent-in-zone
GCP_PROJECT=edgent-app-prod

# ---------------------------------------------------------------------------
# Step 1 — two certificates, in two regions, for ONE hostname
# ---------------------------------------------------------------------------
# This surprises everyone once: you need TWO ACM certificates for the same name.
#
#   CloudFront  MUST have its certificate in us-east-1. Always. Regardless of
#               where anything else lives. CloudFront is a global service and
#               its control plane is in us-east-1.
#   ALB         needs a REGIONAL certificate, in the ALB's own region.
#
# Certificates are free, so this costs nothing but confusion. Request both.
aws acm request-certificate --region us-east-1 \
  --domain-name "$DOMAIN" --validation-method DNS \
  --tags Key=Project,Value=edgentrag-v3 Key=Purpose,Value=cloudfront

aws acm request-certificate --region "$AWS_REGION" \
  --domain-name "$DOMAIN" --validation-method DNS \
  --tags Key=Project,Value=edgentrag-v3 Key=Purpose,Value=alb

CF_CERT=$(aws acm list-certificates --region us-east-1 \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn|[0]" --output text)
ALB_CERT=$(aws acm list-certificates --region "$AWS_REGION" \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn|[0]" --output text)

# ---------------------------------------------------------------------------
# Step 2 — validation: ONE DNS record satisfies BOTH certificates
# ---------------------------------------------------------------------------
# Ask each certificate for its validation record and compare them. They are
# IDENTICAL — same name, same value:
#
#   _05eab2fe...study.edgent.in.  CNAME  _68a84f39....acm-validations.aws.
#
# ACM derives the record deterministically from (domain name, account id), not
# from the certificate. So one CNAME validates every certificate you ever
# request for this hostname in this account, in any region. Create it once.
aws acm describe-certificate --region us-east-1 --certificate-arn "$CF_CERT" \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
aws acm describe-certificate --region "$AWS_REGION" --certificate-arn "$ALB_CERT" \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'

# Create it in Cloud DNS. Note the trailing dots — Cloud DNS wants fully
# qualified names, and ACM hands them to you that way already.
gcloud dns record-sets create "_05eab2fe7501cbda182a155a1b01647e.${DOMAIN}." \
  --zone="$GCP_ZONE" --project="$GCP_PROJECT" \
  --type=CNAME --ttl=300 \
  --rrdatas="_68a84f39f9826c87fd6bf2714805e341.wzccmgtwzk.acm-validations.aws."

# Confirm the wider internet can see it before blaming ACM for being slow.
# Cloud DNS propagated in seconds here; both certs were ISSUED within minutes.
dig +short CNAME "_05eab2fe7501cbda182a155a1b01647e.${DOMAIN}" @8.8.8.8

aws acm wait certificate-validated --region us-east-1     --certificate-arn "$CF_CERT"
aws acm wait certificate-validated --region "$AWS_REGION" --certificate-arn "$ALB_CERT"

# ---------------------------------------------------------------------------
# Step 3 — WAF, and why SCOPE means you may need two of these too
# ---------------------------------------------------------------------------
# A Web ACL has a SCOPE fixed at creation, and the two scopes are incompatible
# objects even with byte-identical rules:
#
#   CLOUDFRONT  must be created in us-east-1; attaches ONLY to distributions
#   REGIONAL    created in the resource's region; attaches to ALB, API Gateway,
#               AppSync, Cognito user pools
#
# You cannot convert one to the other. We ended up creating both — the
# CLOUDFRONT one for the intended design, the REGIONAL one for the fallback.
#
# The rules: three AWS-managed groups plus one rate limit. 2000 requests per
# 5-minute window per IP is generous enough that a person uploading files and
# chatting never notices, and tight enough to stop a runaway script.
#
# Worth telling students: the realistic way a LEGITIMATE user trips this is a
# browser stuck in an SSE reconnect loop against an expired ticket, retrying
# hard. If a real user gets rate-limited, look there first, not at attackers.
cat > /tmp/waf-rules.json <<'EOF'
[
  {"Name":"AWSCommon","Priority":1,
   "Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesCommonRuleSet"}},
   "OverrideAction":{"None":{}},
   "VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSCommon"}},
  {"Name":"AWSKnownBadInputs","Priority":2,
   "Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesKnownBadInputsRuleSet"}},
   "OverrideAction":{"None":{}},
   "VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSKnownBadInputs"}},
  {"Name":"AWSIpReputation","Priority":3,
   "Statement":{"ManagedRuleGroupStatement":{"VendorName":"AWS","Name":"AWSManagedRulesAmazonIpReputationList"}},
   "OverrideAction":{"None":{}},
   "VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"AWSIpReputation"}},
  {"Name":"RateLimitPerIP","Priority":4,
   "Statement":{"RateBasedStatement":{"Limit":2000,"AggregateKeyType":"IP"}},
   "Action":{"Block":{}},
   "VisibilityConfig":{"SampledRequestsEnabled":true,"CloudWatchMetricsEnabled":true,"MetricName":"RateLimitPerIP"}}
]
EOF

# For the intended design (attach in step 4):
aws wafv2 create-web-acl --region us-east-1 --scope CLOUDFRONT \
  --name edgentrag-v3 \
  --description "EdgentRAG v3 edge protection" \
  --default-action Allow={} --rules file:///tmp/waf-rules.json \
  --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=edgentragV3 \
  --tags Key=Project,Value=edgentrag-v3

# For the fallback:
# (The --description regex rejects parentheses. Plain words only; the error
#  message does at least print the pattern it wanted.)
aws wafv2 create-web-acl --region "$AWS_REGION" --scope REGIONAL \
  --name edgentrag-v3-alb \
  --description "EdgentRAG v3 ALB protection - interim until CloudFront is available" \
  --default-action Allow={} --rules file:///tmp/waf-rules.json \
  --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=edgentragV3Alb \
  --tags Key=Project,Value=edgentrag-v3

WAF_REGIONAL=$(aws wafv2 list-web-acls --region "$AWS_REGION" --scope REGIONAL \
  --query "WebACLs[?Name=='edgentrag-v3-alb'].ARN|[0]" --output text)
ALB=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --names edgentrag-v3 \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# Association fails with WAFUnavailableEntityException while the ALB is still
# provisioning. That error says "Retry your request" and means it literally —
# it is not a permissions problem. Two retries were enough here.
for i in 1 2 3 4 5; do
  aws wafv2 associate-web-acl --region "$AWS_REGION" \
    --web-acl-arn "$WAF_REGIONAL" --resource-arn "$ALB" && break
  sleep 15
done
aws wafv2 get-web-acl-for-resource --region "$AWS_REGION" --resource-arn "$ALB" --query 'WebACL.Name'

# ---------------------------------------------------------------------------
# Step 4 — CloudFront: the config, and the wall we hit
# ---------------------------------------------------------------------------
# THE BLOCK:
#
#   AccessDenied when calling CreateDistribution: Your account must be verified
#   before you can add new CloudFront resources. To verify your account, please
#   contact AWS Support.
#
# A new-account restriction, same family as the 1 vCPU quota in task 8. It
# cannot be resolved from the CLI — it needs a support case. Two independent
# service restrictions on one fresh account is a genuinely useful lesson:
# BUDGET ACCOUNT-READINESS TIME BEFORE A DEADLINE. Provisioning is fast;
# getting an account allowed to provision is not.
#
# The configuration below is what we would have applied, and it is correct.
# Three behaviours, evaluated most-specific-first:
#
#   /api/sessions/*/events   CachingDisabled  + AllViewer   <- the SSE stream
#   /api/*                   CachingDisabled  + AllViewer
#   Default (*)              CachingOptimized               <- the static bundle
#
# ALLVIEWER ON BOTH API BEHAVIOURS IS THE SINGLE EASIEST THING TO GET WRONG.
# CloudFront's default origin-request policy STRIPS most headers and query
# strings before forwarding. Without AllViewer:
#
#   * /api/*  loses `Authorization: Bearer ...`, so every authenticated call
#     fails at the edge with a 401 that has nothing to do with your backend
#   * the events path loses `?ticket=`, so SSE 403s immediately
#
# The events path needs its OWN behaviour rather than being folded into /api/*
# because it must never be compressed or buffered — and keeping it separate
# makes the intent explicit to whoever reads the distribution next.
#
# The managed policy ids below are the same in every AWS account:
#   CachingOptimized  658327ea-f89d-4fab-a63d-7e88639e58f6
#   CachingDisabled   4135ea2d-6df8-44a3-9df3-4b5a84be39ad
#   AllViewer         216adef6-5c7f-47e4-b989-5492eafa07d3
#
# OriginReadTimeout 60 (default 30): margin above the app's 15-second SSE
# heartbeat, the CloudFront counterpart to the ALB's idle timeout from task 8.
#
#   aws cloudfront create-distribution --distribution-config file:///tmp/cf-dist.json
#
# Once it succeeds, the ONLY other change is repointing the CNAME in step 5
# from the ALB to the distribution domain, and then tightening the ALB's
# security group to CloudFront's managed prefix list so nobody can bypass the
# edge — and therefore WAF — by hitting the ALB's DNS name directly.

# ---------------------------------------------------------------------------
# Step 5 — DNS
# ---------------------------------------------------------------------------
# WHY A SUBDOMAIN AND NOT THE APEX. DNS forbids a CNAME at a zone apex, and
# both ALB and CloudFront are CNAME-only targets (no static IPs, so no A
# record). Route 53 works around this with ALIAS records; Cloud DNS has no
# equivalent that can point at an AWS endpoint. So: study.edgent.in, not
# edgent.in. This domain's apex is already an A record for Firebase hosting
# anyway, which is the common real-world reason the question never arises.
ALB_DNS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --names edgentrag-v3 \
  --query 'LoadBalancers[0].DNSName' --output text)

gcloud dns record-sets create "${DOMAIN}." \
  --zone="$GCP_ZONE" --project="$GCP_PROJECT" \
  --type=CNAME --ttl=300 --rrdatas="${ALB_DNS}."

# When CloudFront is available, this becomes an update rather than a create:
#   gcloud dns record-sets update "${DOMAIN}." --zone=$GCP_ZONE --project=$GCP_PROJECT \
#     --type=CNAME --ttl=300 --rrdatas="dxxxxxxxxxxxxx.cloudfront.net."
# TTL 300 is deliberate: a 5-minute TTL makes that cutover quick to roll back.

# ---------------------------------------------------------------------------
# Step 6 — verify, and the ONE test that matters most
# ---------------------------------------------------------------------------
curl -s -w "\nHTTP %{http_code}  TLS %{ssl_verify_result}\n" "https://${DOMAIN}/api/health"
# -> {"status":"ok","service":"api","env":"aws"}   HTTP 200   TLS 0
#    TLS 0 means the certificate verified; no -k anywhere.

curl -s -o /dev/null -w "HTTP %{http_code}  %{content_type}\n" "https://${DOMAIN}/"
# -> HTTP 200  text/html     the built React bundle

curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" "http://${DOMAIN}/api/health"
# -> 301 -> https://study.edgent.in:443/api/health
#    This is the test that caught the missing port-80 security-group rule in
#    task 8. Before the fix it HUNG rather than failing, because the packets
#    were dropped, not refused.

# THE HEADER TEST. Read the response text, not the status code:
curl -s -H "Authorization: Bearer nonsense" "https://${DOMAIN}/api/config/services"
#
#   {"detail":"unreadable token"}     <- CORRECT. The header ARRIVED and
#                                        shared/auth.py::_verify tried to parse
#                                        it. Auth is wired end to end.
#
#   {"detail":"missing bearer token"} <- WRONG. _claims() saw an empty
#                                        Authorization header: something in
#                                        front of the API stripped it. With
#                                        CloudFront, that means AllViewer is
#                                        missing from the /api/* behaviour.
#
# Both are HTTP 401. A test that only checks the status code passes in both
# cases and tells you nothing. This is the difference between testing that
# something failed and testing WHY.

# ---------------------------------------------------------------------------
# Running costs, so nobody is surprised
# ---------------------------------------------------------------------------
#   ALB                 ~$16-20/month just to exist, before traffic
#   WAF Web ACL         $5/month each, plus ~$1/month per rule
#                       (we have two ACLs: delete the unused CLOUDFRONT one if
#                        verification is going to take a while)
#   RDS + ElastiCache   the other standing cost; see task 2
#   Fargate workers     per-second, min 1 task each
#   ACM certificates    free
#
# The cheapest pause between teaching sessions, keeping all configuration:
#   aws autoscaling update-auto-scaling-group --auto-scaling-group-name edgentrag-v3 \
#     --min-size 0 --desired-capacity 0
#   aws ecs update-service --cluster edgentrag-v3 --service edgentrag-v3-ingest --desired-count 0
#   aws ecs update-service --cluster edgentrag-v3 --service edgentrag-v3-chat   --desired-count 0
# The ALB, RDS and ElastiCache keep billing; delete those to stop it entirely.
#
# ---------------------------------------------------------------------------
# WHAT IS LEFT: start the three Colab services (services/README.md), sign in at
# https://study.edgent.in, and paste the three tunnel addresses into the connect
# screen. That last step needs `admins` group membership — see task 5.
