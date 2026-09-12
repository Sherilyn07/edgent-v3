# The frontend: built to static files, then served by nginx on the same EC2
# box as the API (docker-compose.ec2.yml) -- not S3+CloudFront static
# hosting. CloudFront still sits in front of the whole thing (static assets
# and /api/* both), with the ALB as its origin; see DEPLOY.md.
#
# Two stages, same as version 2. The first has Node and builds the bundle;
# the second is nginx and the bundle, nothing else. Build from the v3 repo
# root, same as every other image here, so docker/nginx.conf is in context:
#
#   docker build -f frontend/Dockerfile -t edgentrag/web .

FROM node:20-alpine AS build
WORKDIR /app

COPY frontend/package.json ./
# `npm install`, not `npm ci`: there is no lockfile checked in for v3 (a new
# dependency, amazon-cognito-identity-js, was added and the lockfile was not
# regenerated in this environment). Run `npm install` once locally and commit
# the resulting package-lock.json to switch this back to `npm ci`, which is
# the faster, more reproducible choice once one exists.
RUN npm install --no-audit --no-fund

COPY frontend/ ./
# VITE_COGNITO_* and VITE_API_BASE are baked in here, at build time, from
# .env.production -- fill in the Cognito values from DEPLOY.md's Cognito
# section before building this image.
RUN npm run build


FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
