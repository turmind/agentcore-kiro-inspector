#!/usr/bin/env bash
# Build the arm64 image and push it to ECR.
#
#   REGION=<REGION> TAG=<TAG> scripts/build_push.sh
#
# The repository is created with scan-on-push and NO repository policy: pull
# access stays IAM-only inside this account (the runtime's execution role).
# Do not add a repository policy.
#
# Prereqs (one time on an x86 build host):
#   docker run --privileged --rm tonistiigi/binfmt --install arm64
#   build/kiro/bin/kiro-cli-chat in place (see wecom-eventbridge-deploy.md, 步骤 2)
#
# Prints the pushed image URI on the last line.
set -euo pipefail

REGION="${REGION:?set REGION, e.g. REGION=ap-northeast-1}"
REPO="${REPO:-bedrock-agentcore-kiro-inspector}"
TAG="${TAG:?set TAG, e.g. TAG=v5 (use a new tag per build; runtime updates pin a tag)}"
BUILDER="${BUILDER:-default}"

here="$(cd "$(dirname "$0")/.." && pwd)"
bin="$here/build/kiro/bin/kiro-cli-chat"
[ -x "$bin" ] || { echo "missing $bin" >&2; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
URI="$REGISTRY/$REPO:$TAG"

# Repository (idempotent). Scan on push is free for basic scanning.
aws ecr describe-repositories --region "$REGION" --repository-names "$REPO" >/dev/null 2>&1 \
  || aws ecr create-repository --region "$REGION" --repository-name "$REPO" \
       --image-scanning-configuration scanOnPush=true >/dev/null

# Login: write the password to a file first. Piping get-login-password straight
# into `docker login` fails in non-TTY shells (SSM / CI) with
# "Cannot perform an interactive login from a non TTY device".
pw="$(mktemp)"; trap 'rm -f "$pw"' EXIT
aws ecr get-login-password --region "$REGION" > "$pw"
docker login --username AWS --password-stdin "$REGISTRY" < "$pw"

docker buildx build --builder "$BUILDER" --platform linux/arm64 \
  -t "$URI" --push "$here/build"

aws ecr describe-images --region "$REGION" --repository-name "$REPO" \
  --image-ids imageTag="$TAG" --query 'imageDetails[0].[imageDigest,imageSizeInBytes]' --output text
echo "$URI"
