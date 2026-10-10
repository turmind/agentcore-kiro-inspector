# AgentCore Runtime requires linux/arm64.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim-bookworm

# kiro-cli-chat is a dynamically linked glibc binary; it needs ca-certificates
# for TLS to the Kiro API and to AWS endpoints. Report delivery (notify.py) is
# stdlib plus `websockets` (WeCom 智能机器人 long connection); curl/jq stay so
# the agent has them for ad-hoc shell checks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl jq unzip \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (arm64). The v1 engine's built-in `use_aws` tool does NOT need this,
# but having the CLI on PATH lets the agent fall back to shelling out, and is
# required if you ever switch to --agent-engine v3 (where aws_tool was removed).
RUN curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscli.zip /tmp/aws \
    && aws --version

RUN pip install --no-cache-dir flask==3.1.0 websockets==16.0

# Original, unmodified Kiro CLI payload (aarch64 glibc build, v2.21.4).
# Only kiro-cli-chat is copied: it is the binary that serves `chat`, and it works
# standalone (verified). kiro-cli (launcher) and kiro-cli-term (PTY) are not needed.
COPY kiro/bin/kiro-cli-chat /opt/kiro/bin/kiro-cli-chat
RUN chmod +x /opt/kiro/bin/kiro-cli-chat

COPY server.py notify.py /app/

ENV KIRO_BIN=/opt/kiro/bin/kiro-cli-chat \
    KIRO_MODEL=gpt-5.6-luna \
    KIRO_EFFORT=max \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# Kiro writes session/config state under $HOME; each run gets its own temp HOME,
# but give the default one a writable place too.
ENV HOME=/tmp

EXPOSE 8080

CMD ["python3", "/app/server.py"]
