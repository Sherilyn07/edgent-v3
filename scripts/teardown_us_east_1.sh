#!/usr/bin/env bash
#
# Tear down everything docs/guides/06-aws-console-us-east-1.md creates.
#
# Built by hand in the console, deleted from the CLI: every resource is found
# by the NAME the guide gave it, so it does not matter how it was created.
#
#   bash scripts/teardown_us_east_1.sh              # asks you to type "delete"
#   DOMAIN=study.example.com bash scripts/teardown_us_east_1.sh   # also removes the ACM cert
#
# Safe to re-run: anything already gone is reported as "skip" and the script
# moves on. It never touches resources outside the edgentrag-v3 names below.
#
# NOT deleted (do these by hand):
#   - the DNS record at your DNS provider (CNAME / alias to the ALB or CloudFront)
#   - the ACM validation CNAME at your DNS provider
#   - a CloudFront distribution (must be disabled and fully deployed first; see step 1)
#   - AWS service-linked roles (AWSServiceRoleForECS etc.) — shared, harmless, free
#   - your GitHub repo

set -uo pipefail

export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_PAGER=""
P=edgentrag-v3

ACC=$(aws sts get-caller-identity --query Account --output text) || { echo "AWS CLI not logged in"; exit 1; }

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   deleted  %s\n' "$*"; }
skip() { printf '   skip     %s\n' "$*"; }
# run a command; report ok/skip instead of stopping the whole teardown
try()  { local what=$1; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else skip "$what"; fi; }

echo "Account : $ACC"
echo "Region  : $AWS_REGION"
echo "This PERMANENTLY deletes every $P resource: database, cache, bucket contents,"
echo "queues, secrets, users in the Cognito pool, images, logs."
read -r -p 'Type "delete" to continue: ' answer
[ "$answer" = "delete" ] || { echo "aborted"; exit 1; }

# Read the Cognito pool id NOW: step 12 deletes the parameter that holds it,
# and the console wizard names pools "User pool - xxxx", so a name lookup
# alone would miss it.
POOL_FROM_SSM=$(aws ssm get-parameter --name /$P/cognito-user-pool-id --query Parameter.Value --output text 2>/dev/null)

# ---------------------------------------------------------------------------
say "1. CloudFront (check only)"
CF=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Origins.Items[0].DomainName, '$P')].Id" --output text 2>/dev/null)
if [ -n "$CF" ] && [ "$CF" != "None" ]; then
  echo "   Distribution(s) still exist: $CF"
  echo "   Console: CloudFront → select → Disable → wait until 'Deployed' → Delete."
  echo "   (Continuing; the ALB can be deleted regardless.)"
else
  skip "no CloudFront distribution"
fi

# ---------------------------------------------------------------------------
say "2. Auto Scaling group + launch template (the EC2 servers)"
if aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names $P \
     --query 'AutoScalingGroups[0].AutoScalingGroupName' --output text 2>/dev/null | grep -q $P; then
  aws autoscaling delete-auto-scaling-group --auto-scaling-group-name $P --force-delete >/dev/null
  echo "   waiting for instances to terminate..."
  while aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names $P \
          --query 'AutoScalingGroups[0].AutoScalingGroupName' --output text 2>/dev/null | grep -q $P; do sleep 15; done
  ok "auto scaling group $P"
else
  skip "auto scaling group $P"
fi
try "launch template $P" aws ec2 delete-launch-template --launch-template-name $P

# ---------------------------------------------------------------------------
say "3. WAF (regional web ACL on the ALB)"
ALB_ARN=$(aws elbv2 describe-load-balancers --names $P --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
[ "$ALB_ARN" = "None" ] && ALB_ARN=""
[ -n "$ALB_ARN" ] && try "WAF association on ALB" aws wafv2 disassociate-web-acl --resource-arn "$ALB_ARN"
for scope in REGIONAL; do
  for name in $P-alb $P; do
    read -r ID LOCK < <(aws wafv2 list-web-acls --scope $scope \
      --query "WebACLs[?Name=='$name'].[Id,LockToken]" --output text 2>/dev/null)
    if [ -n "${ID:-}" ] && [ "$ID" != "None" ]; then
      try "web ACL $name ($scope)" aws wafv2 delete-web-acl --scope $scope --name $name --id "$ID" --lock-token "$LOCK"
    fi
    ID=""; LOCK=""
  done
done
echo "   (A CLOUDFRONT-scope web ACL is deleted with/after the distribution, in the console.)"

# ---------------------------------------------------------------------------
say "4. Load balancer + target group"
if [ -n "$ALB_ARN" ]; then
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" >/dev/null && ok "ALB $P"
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN" 2>/dev/null
else
  skip "ALB $P"
fi
TG=$(aws elbv2 describe-target-groups --names $P-api --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)
if [ -n "$TG" ] && [ "$TG" != "None" ]; then
  # a target group can linger "in use" for a few seconds after its ALB goes
  for i in 1 2 3 4 5 6; do aws elbv2 delete-target-group --target-group-arn "$TG" >/dev/null 2>&1 && break; sleep 10; done
  ok "target group $P-api"
else
  skip "target group $P-api"
fi

# ---------------------------------------------------------------------------
say "5. ECS: autoscaling, services, task definitions, cluster"
for s in ingest chat; do
  try "scalable target $P-$s" aws application-autoscaling deregister-scalable-target \
    --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
    --resource-id service/$P/$P-$s
  aws ecs update-service --cluster $P --service $P-$s --desired-count 0 >/dev/null 2>&1
  try "service $P-$s" aws ecs delete-service --cluster $P --service $P-$s --force
done
aws ecs wait services-inactive --cluster $P --services $P-ingest $P-chat 2>/dev/null
for fam in $P-migrate $P-ingest $P-chat; do
  REVS=$(aws ecs list-task-definitions --family-prefix $fam --status ACTIVE --query 'taskDefinitionArns' --output text 2>/dev/null)
  for r in $REVS; do aws ecs deregister-task-definition --task-definition "$r" >/dev/null 2>&1; done
  ALL=$(aws ecs list-task-definitions --family-prefix $fam --status INACTIVE --query 'taskDefinitionArns' --output text 2>/dev/null)
  if [ -n "$ALL" ]; then
    # delete-task-definitions takes at most 10 at a time
    echo $ALL | xargs -n 10 aws ecs delete-task-definitions --task-definitions >/dev/null 2>&1
    ok "task definitions $fam"
  else
    skip "task definitions $fam"
  fi
done
# a stopped migrate task can hold the cluster briefly
for t in $(aws ecs list-tasks --cluster $P --query 'taskArns' --output text 2>/dev/null); do
  aws ecs stop-task --cluster $P --task "$t" >/dev/null 2>&1
done
try "cluster $P" aws ecs delete-cluster --cluster $P

for a in ingest-backlog-high ingest-backlog-low; do
  try "alarm $a" aws cloudwatch delete-alarms --alarm-names $a
done

# ---------------------------------------------------------------------------
say "6. ECR repositories (and their images)"
for r in api ingest-worker chat-worker; do
  try "repo $P/$r" aws ecr delete-repository --repository-name $P/$r --force
done

# ---------------------------------------------------------------------------
say "7. CloudWatch log groups"
for g in migrate ingest chat; do
  try "log group /ecs/$P-$g" aws logs delete-log-group --log-group-name /ecs/$P-$g
done

# ---------------------------------------------------------------------------
say "8. RDS + ElastiCache (start both deletions, then wait)"
RDS_EXISTS=$(aws rds describe-db-instances --db-instance-identifier $P --query 'DBInstances[0].DBInstanceIdentifier' --output text 2>/dev/null)
if [ "$RDS_EXISTS" = "$P" ]; then
  aws rds modify-db-instance --db-instance-identifier $P --no-deletion-protection --apply-immediately >/dev/null 2>&1
  aws rds delete-db-instance --db-instance-identifier $P --skip-final-snapshot --delete-automated-backups >/dev/null \
    && echo "   RDS deletion started"
else
  skip "RDS $P"
fi
if aws elasticache describe-replication-groups --replication-group-id $P >/dev/null 2>&1; then
  aws elasticache delete-replication-group --replication-group-id $P >/dev/null && echo "   ElastiCache deletion started"
else
  skip "ElastiCache $P"
fi
if [ "$RDS_EXISTS" = "$P" ]; then
  echo "   waiting for RDS (5-10 min)..."
  aws rds wait db-instance-deleted --db-instance-identifier $P && ok "RDS $P"
fi
echo "   waiting for ElastiCache (5-10 min)..."
aws elasticache wait replication-group-deleted --replication-group-id $P 2>/dev/null && ok "ElastiCache $P"
try "DB subnet group $P-db"       aws rds delete-db-subnet-group --db-subnet-group-name $P-db
try "cache subnet group $P-cache" aws elasticache delete-cache-subnet-group --cache-subnet-group-name $P-cache

# ---------------------------------------------------------------------------
say "9. SQS queues (8)"
for q in ingest chat stt embed; do
  for name in $P-$q $P-$q-dlq; do
    URL=$(aws sqs get-queue-url --queue-name $name --query QueueUrl --output text 2>/dev/null)
    if [ -n "$URL" ] && [ "$URL" != "None" ]; then try "queue $name" aws sqs delete-queue --queue-url "$URL"; else skip "queue $name"; fi
  done
done

# ---------------------------------------------------------------------------
say "10. S3 bucket (emptied first)"
BUCKET=$P-$ACC
if aws s3api head-bucket --bucket $BUCKET >/dev/null 2>&1; then
  aws s3 rm s3://$BUCKET --recursive --only-show-errors
  try "bucket $BUCKET" aws s3api delete-bucket --bucket $BUCKET
else
  skip "bucket $BUCKET"
fi

# ---------------------------------------------------------------------------
say "11. Secrets Manager (no 30-day recovery window)"
for s in database-url redis-url broker-token; do
  try "secret $P/$s" aws secretsmanager delete-secret --secret-id $P/$s --force-delete-without-recovery
done

say "12. Parameter Store"
for n in s3-bucket aws-region ingest-queue-url chat-queue-url stt-queue-url embed-queue-url \
         cognito-region cognito-user-pool-id cognito-app-client-id; do
  try "/$P/$n" aws ssm delete-parameter --name /$P/$n
done

# ---------------------------------------------------------------------------
say "13. Cognito user pool (and every user in it)"
POOL=${POOL_FROM_SSM:-}
if [ -z "$POOL" ] || [ "$POOL" = "None" ]; then
  POOL=$(aws cognito-idp list-user-pools --max-results 60 --query "UserPools[?Name=='$P'].Id | [0]" --output text 2>/dev/null)
fi
[ "$POOL" = "None" ] && POOL=""
if [ -n "$POOL" ]; then
  # the console wizard creates a login domain; a pool with a domain cannot be deleted
  DOM=$(aws cognito-idp describe-user-pool --user-pool-id "$POOL" --query 'UserPool.Domain' --output text 2>/dev/null)
  [ -n "$DOM" ] && [ "$DOM" != "None" ] && try "cognito domain $DOM" aws cognito-idp delete-user-pool-domain --user-pool-id "$POOL" --domain "$DOM"
  aws cognito-idp update-user-pool --user-pool-id "$POOL" --deletion-protection INACTIVE >/dev/null 2>&1
  try "user pool $POOL" aws cognito-idp delete-user-pool --user-pool-id "$POOL"
else
  skip "user pool (not in Parameter Store, none named '$P')"
  echo "   If the console named it differently: Cognito → User pools → select → Delete."
fi

# ---------------------------------------------------------------------------
say "14. IAM roles + instance profile"
del_role() {
  local role=$1
  aws iam get-role --role-name "$role" >/dev/null 2>&1 || { skip "role $role"; return; }
  for p in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$p"
  done
  for a in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$a"
  done
  for ip in $(aws iam list-instance-profiles-for-role --role-name "$role" --query 'InstanceProfiles[].InstanceProfileName' --output text); do
    aws iam remove-role-from-instance-profile --instance-profile-name "$ip" --role-name "$role"
    aws iam delete-instance-profile --instance-profile-name "$ip" && ok "instance profile $ip"
  done
  try "role $role" aws iam delete-role --role-name "$role"
}
for r in $P-ec2 $P-ingest-task-role $P-chat-task-role $P-execution-role; do del_role $r; done

# ---------------------------------------------------------------------------
say "15. ACM certificate"
if [ -n "${DOMAIN:-}" ]; then
  CERT=$(aws acm list-certificates --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" --output text)
  if [ -n "$CERT" ] && [ "$CERT" != "None" ]; then
    try "certificate $DOMAIN" aws acm delete-certificate --certificate-arn "$CERT"
  else
    skip "certificate $DOMAIN"
  fi
else
  skip "certificate (set DOMAIN=your.domain to delete it)"
fi

# ---------------------------------------------------------------------------
say "16. Security groups (last: RDS/Redis/Fargate network interfaces must be gone)"
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
sg_id() { aws ec2 describe-security-groups --filters Name=vpc-id,Values=$VPC Name=group-name,Values=$1 \
            --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null; }
# 1) remove every inbound rule, so no group references another
for n in alb ec2 ecs rds redis; do
  ID=$(sg_id $P-$n)
  [ -z "$ID" ] || [ "$ID" = "None" ] && continue
  RULES=$(aws ec2 describe-security-group-rules --filters Name=group-id,Values=$ID \
    --query 'SecurityGroupRules[?IsEgress==`false`].SecurityGroupRuleId' --output text)
  [ -n "$RULES" ] && aws ec2 revoke-security-group-ingress --group-id "$ID" --security-group-rule-ids $RULES >/dev/null 2>&1
done
# 2) delete; network interfaces from deleted RDS/ElastiCache/Fargate can take a few minutes to release
for n in rds redis ecs ec2 alb; do
  ID=$(sg_id $P-$n)
  if [ -z "$ID" ] || [ "$ID" = "None" ]; then skip "security group $P-$n"; continue; fi
  for i in $(seq 1 20); do
    if aws ec2 delete-security-group --group-id "$ID" >/dev/null 2>&1; then ok "security group $P-$n"; break; fi
    [ "$i" = 20 ] && { skip "security group $P-$n (still in use — re-run the script in a few minutes)"; break; }
    sleep 15
  done
done

# ---------------------------------------------------------------------------
say "Done"
echo "Left for you to do by hand:"
echo "  - DNS: delete the CNAME/alias for your domain and the ACM validation CNAME"
echo "  - CloudFront distribution + its CLOUDFRONT web ACL, if you created them"
echo "Verify nothing is left:"
echo "  aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=$P --query 'ResourceTagMappingList[].ResourceARN'"
