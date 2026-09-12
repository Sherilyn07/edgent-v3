#!/usr/bin/env bash
#
# Task 5 of the EdgentRAG v3 deployment — Cognito (identity).
#
# Every command below was executed successfully against account ${ACC}
# in ap-southeast-1 on 2026-09-12. Nothing here is untested or aspirational.
#
# WHY THIS IS TASK 5 AND NOT DEPLOY.md's SECTION 8
# ------------------------------------------------
# DEPLOY.md puts Cognito after the EC2 Auto Scaling Group, then admits the
# cost: "Rebuild and redeploy the web image after filling these in."
#
# The reason is an asymmetry in how the two halves read the same three values:
#
#   backend (api)   reads COGNITO_*      at CONTAINER START  -> restart picks it up
#   frontend (web)  reads VITE_COGNITO_* at `vite build`     -> BAKED INTO THE BUNDLE
#
# Vite has no server side. Whatever VITE_* held at build time is compiled into
# the JavaScript and cannot be changed afterwards without rebuilding. Create
# the pool first and the EC2 launch is also the last one.
#
# General lesson: before deploying anything, ask of each config value "is this
# read at build time or at run time?" Build-time values constrain your
# ordering; run-time values do not.
#
# WHAT COGNITO IS DOING HERE
# --------------------------
# Sign-up, email confirmation, passwords, reset, and token issuing — all of it.
# Which is why shared/models.py has four tables and none of them is `users`.
# Cognito owns the people; the app stores exactly one string per session:
#
#   Session.owner_id = claims["sub"]        # routes/sessions.py
#   if session.owner_id != owner_id: 403    # every session route
#
# That check is the whole of v3's authorization, and the thing v2 had no
# answer for — there, a session id WAS the identity, so anyone holding one
# could read someone else's files.
#
# HOW TO RUN
#   bash scripts/create_cognito.sh

set -euo pipefail

export AWS_REGION=ap-southeast-1

# Your AWS account id, looked up rather than hardcoded — so this script works
# in whatever account your credentials point at, not just the one it was
# written in.
ACC=$(aws sts get-caller-identity --query Account --output text)

# ---------------------------------------------------------------------------
# Step 1 — the user pool
# ---------------------------------------------------------------------------
# Read the flags against frontend/src/auth.js; every one of them is a response
# to something that file does.
#
#   --username-attributes email
#       Makes the email address the username itself. auth.js::signUp passes the
#       email as BOTH the Username and the email attribute, so this is the
#       setting that makes those consistent. The alternative (separate username
#       + email alias) would need frontend changes.
#
#   --auto-verified-attributes email
#       Cognito emails a confirmation code on sign-up. This is what makes
#       auth.js::confirmSignUp's `user.confirmRegistration(code, ...)` a real
#       flow rather than dead code. Without it, accounts are created
#       unconfirmed and sign-in fails with an error naming nothing useful.
#
#   --policies
#       8 chars, upper + lower + number, no symbol required. Cognito's default
#       demands symbols too; dropping that is a deliberate, small relaxation
#       for a course project. Everything else stays.
#
#   --admin-create-user-config AllowAdminCreateUserOnly=false
#       Self-service sign-up ON. Set it true and the app's sign-up screen
#       breaks — only an administrator could create accounts.
#
#   --account-recovery-setting verified_email
#       Forgot-password goes to email. The only channel here; no SMS is
#       configured, and leaving recovery unset produces confusing behaviour.
#
#   --email-configuration COGNITO_DEFAULT
#       Cognito sends the mail itself. Capped around 50 messages/day — fine for
#       a class, and the thing to change (to SES) if that cap is ever hit.
#
# NOT set: no Hosted UI, no domain, no OAuth flows. The frontend talks to
# Cognito directly through amazon-cognito-identity-js, so there is no redirect
# flow and no callback URL to configure.
aws cognito-idp create-user-pool --region "$AWS_REGION" \
  --pool-name edgentrag-v3 \
  --username-attributes email \
  --auto-verified-attributes email \
  --mfa-configuration OFF \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
  --schema '[{"Name":"email","AttributeDataType":"String","Required":true,"Mutable":true}]' \
  --admin-create-user-config '{"AllowAdminCreateUserOnly":false}' \
  --account-recovery-setting '{"RecoveryMechanisms":[{"Priority":1,"Name":"verified_email"}]}' \
  --email-configuration '{"EmailSendingAccount":"COGNITO_DEFAULT"}' \
  --user-pool-tags Project=edgentrag-v3

POOL=ap-southeast-1_2uivSAqhZ     # <- from the output above; yours will differ

# ---------------------------------------------------------------------------
# Step 2 — the app client
# ---------------------------------------------------------------------------
#   --no-generate-secret
#       THE important one. A client secret cannot be kept secret in browser
#       JavaScript — it would ship inside the bundle, readable by anyone who
#       opens devtools. amazon-cognito-identity-js refuses to work with one,
#       and shared/auth.py never expects one. Verify: HasSecret must be null
#       in the output.
#
#   --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH
#       Exactly the two flows auth.js uses, and no more:
#         USER_SRP_AUTH   <- authenticateUser(). SRP means the password is
#                            never sent over the wire, even inside TLS; the
#                            client proves knowledge of it instead.
#         REFRESH_TOKEN   <- getSession()'s silent refresh, which is how a
#                            signed-in browser stays signed in.
#       Deliberately absent: ALLOW_USER_PASSWORD_AUTH, which sends the plain
#       password to Cognito. Nothing here needs it, so it is not enabled.
#
#   token validity 1h / 1h / 30 days
#       A short ID token is safe precisely BECAUSE the refresh token is long:
#       getSession() renews silently, so the credential attached to every
#       request is short-lived without anyone being logged out hourly.
#
#   --prevent-user-existence-errors ENABLED
#       "Incorrect username or password" for both wrong-password and
#       no-such-user. Otherwise the error messages differ and the endpoint
#       becomes a way to test whether an email has an account here.
aws cognito-idp create-user-pool-client --region "$AWS_REGION" \
  --user-pool-id "$POOL" \
  --client-name edgentrag-v3-web \
  --no-generate-secret \
  --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
  --id-token-validity 1 --access-token-validity 1 --refresh-token-validity 30 \
  --token-validity-units '{"IdToken":"hours","AccessToken":"hours","RefreshToken":"days"}' \
  --prevent-user-existence-errors ENABLED

CLIENT=622ntbu6tsln0tn19rpogq71bm    # <- from the output above; yours will differ

# ---------------------------------------------------------------------------
# Step 3 — the admins group
# ---------------------------------------------------------------------------
# The name must be exactly "admins". shared/auth.py::require_admin does:
#
#     groups = claims.get("cognito:groups") or []
#     if "admins" not in groups: raise HTTPException(403, ...)
#
# It guards ONE endpoint: PUT /config/services, which repoints where
# embedding/STT/LLM traffic goes. v2 left that wide open — anyone who could
# reach the API could redirect every user's documents to a server of their
# choosing.
#
# GET /config/services stays require_user, NOT require_admin. Every signed-in
# user needs to know whether the services are ready before uploading; gating
# the read too leaves ordinary users stuck on the connect screen forever. The
# docstring in routes/config.py notes an earlier pass made exactly that
# mistake — worth showing students, because "lock down the whole router" is
# the instinct and it is wrong here.
aws cognito-idp create-group --region "$AWS_REGION" \
  --user-pool-id "$POOL" --group-name admins \
  --description "Members may PUT /config/services (repoint the Colab addresses). See shared/auth.py::require_admin."

# ---------------------------------------------------------------------------
# Step 4 — publish the three values to BOTH sides
# ---------------------------------------------------------------------------
# None of these is a secret. A pool id and a client id are visible in any
# browser that loads the app — they identify the pool, they do not authorise
# anything. Hence SSM String parameters, not Secrets Manager.

# Backend: read by the EC2 launch template's user data at boot.
aws ssm put-parameter --region "$AWS_REGION" --name /edgentrag-v3/cognito-region        --type String --value "$AWS_REGION" --overwrite
aws ssm put-parameter --region "$AWS_REGION" --name /edgentrag-v3/cognito-user-pool-id  --type String --value "$POOL"       --overwrite
aws ssm put-parameter --region "$AWS_REGION" --name /edgentrag-v3/cognito-app-client-id --type String --value "$CLIENT"     --overwrite

# Frontend: compiled into the bundle by `vite build`. THIS is the step that
# must happen before the web image is built. (sed -i '' is BSD/macOS; Linux
# is sed -i.)
sed -i '' \
  -e "s#^VITE_COGNITO_REGION=.*#VITE_COGNITO_REGION=$AWS_REGION#" \
  -e "s#^VITE_COGNITO_USER_POOL_ID=.*#VITE_COGNITO_USER_POOL_ID=$POOL#" \
  -e "s#^VITE_COGNITO_APP_CLIENT_ID=.*#VITE_COGNITO_APP_CLIENT_ID=$CLIENT#" \
  frontend/.env.production

grep -E '^VITE_' frontend/.env.production

# ---------------------------------------------------------------------------
# Step 5 — verify
# ---------------------------------------------------------------------------
# The JWKS endpoint is the single most important thing to check, because it is
# what shared/auth.py::_JwksCache fetches to verify every incoming token. It is
# public and unauthenticated by design — these are PUBLIC keys, used to check
# a signature, never to create one.
#
# Expect 2 keys, both RS256. Cognito publishes two so a key rotation does not
# invalidate tokens signed by the outgoing one. _JwksCache handles that: it
# refetches when it sees a `kid` it does not recognise, or hourly.
#
# A 404 here means the pool id is wrong, and every API call would fail with
# "unknown signing key".
curl -s "https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL}/.well-known/jwks.json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('keys:', len(d['keys']), '/ alg:', {k['alg'] for k in d['keys']})"

# The issuer string _verify() checks the token's `iss` claim against. Same
# URL as above minus the well-known path — worth printing once so the shape is
# familiar when a token is rejected for a bad issuer.
echo "https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL}"

# HasSecret MUST be null.
aws cognito-idp describe-user-pool-client --region "$AWS_REGION" \
  --user-pool-id "$POOL" --client-id "$CLIENT" \
  --query 'UserPoolClient.{Name:ClientName,HasSecret:ClientSecret,Flows:ExplicitAuthFlows}' --output json

aws ssm get-parameters-by-path --region "$AWS_REGION" --path /edgentrag-v3 \
  --query 'Parameters[?contains(Name,`cognito`)].{Name:Name,Value:Value}' --output table

# ---------------------------------------------------------------------------
# STILL TO DO — a human step that cannot be scripted yet
# ---------------------------------------------------------------------------
# The admins group exists but is EMPTY. It cannot be otherwise: there are no
# users yet, and the first one is created by signing up through the app, which
# is not deployed. So, once the app is live:
#
#   1. Open the app, sign up, confirm the emailed code, sign in.
#   2. Add yourself:
#        aws cognito-idp admin-add-user-to-group --region $AWS_REGION \
#          --user-pool-id $POOL --group-name admins --username <your-email>
#   3. SIGN OUT AND BACK IN. This step is missed constantly. Group membership
#      is a CLAIM INSIDE the token. The token in the browser was minted before
#      you joined the group, so it does not carry cognito:groups and
#      PUT /config/services keeps returning 403 no matter what the console
#      shows. A new token is the only fix; a fresh sign-in is how you get one.
#
# Until step 3 is done you cannot set the Colab service addresses, which means
# you cannot upload anything.

# ---------------------------------------------------------------------------
# THE ESCAPE HATCH, and why it exists
# ---------------------------------------------------------------------------
# Leave all three values blank and auth turns off entirely, on BOTH sides:
#
#   shared/auth.py::cognito_configured()  -> False, require_user returns "local-dev"
#   frontend/src/auth.js::authConfigured() -> false, App.jsx skips the sign-in gate
#
# Two independent checks that agree without any shared "local mode" flag. That
# is what lets the whole stack run on a laptop before a pool exists. It is a
# development affordance, not a deployment option — never expose a public URL
# running this way.
#
# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
#   aws cognito-idp delete-user-pool-client --region $AWS_REGION --user-pool-id $POOL --client-id $CLIENT
#   aws cognito-idp delete-user-pool --region $AWS_REGION --user-pool-id $POOL
#   aws ssm delete-parameters --region $AWS_REGION --names /edgentrag-v3/cognito-region /edgentrag-v3/cognito-user-pool-id /edgentrag-v3/cognito-app-client-id
#
# Deleting a pool deletes every user in it, irreversibly. There is no recovery
# window, unlike Secrets Manager.
#
# ---------------------------------------------------------------------------
# PHASES A AND B ARE NOW COMPLETE. Everything stateful exists; nothing runs.
#
# NEXT: Phase C — build and push the three images to ECR, create the two task
# roles, then run the Alembic migration as a one-off ECS task. That migration
# is a hard gate: no worker and no EC2 instance may start before it succeeds,
# because nothing in v3 creates schema at startup.
