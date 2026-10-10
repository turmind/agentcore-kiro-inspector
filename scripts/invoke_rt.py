#!/usr/bin/env python3
"""
Invoke the inspector runtime once (SigV4, the caller's own AWS credentials).

  python3 scripts/invoke_rt.py --region <REGION> --runtime-id <RUNTIME_ID> "巡检 ..."
  python3 scripts/invoke_rt.py --region <REGION> --runtime-arn <ARN> --prompt-file p.txt
  python3 scripts/invoke_rt.py ... --no-notify           # just return the report
  python3 scripts/invoke_rt.py ... --async               # accepted at once, pushed later
  python3 scripts/invoke_rt.py ... --session-id <ID>     # same microVM as an earlier call

--runtime-id is turned into the ARN locally (sts get-caller-identity), so the
caller only needs bedrock-agentcore:InvokeAgentRuntime -- no control-plane read.

Unlike `aws bedrock-agentcore invoke-agent-runtime --payload`, non-ASCII
prompts are fine here (the payload is built as UTF-8 bytes).

Output: call metadata on stderr, the report text on stdout (--json: raw body).
"""

import argparse
import json
import os
import sys
import time
import uuid

import boto3
from botocore.config import Config

DEFAULT_PROMPT = (
    "巡检 {region} 区域：1) RDS/Aurora 所有实例与集群的待维护动作（pending maintenance actions），"
    "2) ElastiCache 未完成的服务更新。把结果整理成简明中文报告。"
)


def build_payload(prompt, run_async=False, notify=True, channels=(), thread=None, new_conversation=False):
    payload = {"prompt": prompt}
    if run_async:
        payload["async"] = True
    if not notify:
        payload["notify"] = False
    if channels:
        payload["channels"] = list(channels)
    if thread:
        payload["thread"] = thread
    if new_conversation:
        payload["new_conversation"] = True
    return payload


def new_session_id():
    return "kiro-inspect-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]   # 53 chars; API wants 33-256


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("prompt", nargs="?", help="prompt text (default: RDS/ElastiCache maintenance check)")
    p.add_argument("--prompt-file", help="read the prompt from a UTF-8 file")
    p.add_argument("--region", default=os.environ.get("AWS_REGION"), help="required (or AWS_REGION)")
    p.add_argument("--runtime-arn", default=os.environ.get("AGENT_RUNTIME_ARN"))
    p.add_argument("--runtime-id", default=os.environ.get("RUNTIME_ID"), help="alternative to --runtime-arn")
    p.add_argument("--qualifier", default="DEFAULT", help="runtime endpoint name")
    p.add_argument("--session-id", help="runtimeSessionId, 33-256 chars (default: new random id)")
    p.add_argument("--async", dest="run_async", action="store_true")
    p.add_argument("--no-notify", action="store_true", help="do not push this report to WeCom / Feishu")
    p.add_argument("--channel", action="append", default=[], choices=("wecom", "lark"),
                   help="only these channels (repeatable)")
    p.add_argument("--thread", help="state dir name, decoupled from the session id (session mode)")
    p.add_argument("--new-conversation", action="store_true", help="do not resume the previous conversation")
    p.add_argument("--read-timeout", type=int, default=900)
    p.add_argument("--json", action="store_true", help="print the raw response body")
    a = p.parse_args()

    if not a.region:
        p.error("--region is required (or set AWS_REGION)")
    if not (a.runtime_arn or a.runtime_id):
        p.error("give --runtime-arn or --runtime-id (or AGENT_RUNTIME_ARN / RUNTIME_ID)")
    if a.prompt and a.prompt_file:
        p.error("give the prompt as an argument or --prompt-file, not both")
    if a.session_id and not 33 <= len(a.session_id) <= 256:
        p.error(f"--session-id is {len(a.session_id)} chars; the API wants 33-256")

    if a.prompt_file:
        with open(a.prompt_file, encoding="utf-8") as f:
            prompt = f.read().strip()
    else:
        prompt = a.prompt or DEFAULT_PROMPT.format(region=a.region)

    s = boto3.Session(region_name=a.region)
    arn = a.runtime_arn
    if not arn:
        account = s.client("sts").get_caller_identity()["Account"]
        arn = f"arn:aws:bedrock-agentcore:{a.region}:{account}:runtime/{a.runtime_id}"
    sid = a.session_id or new_session_id()
    payload = build_payload(prompt, a.run_async, not a.no_notify, a.channel, a.thread, a.new_conversation)

    c = s.client("bedrock-agentcore", config=Config(
        read_timeout=a.read_timeout, connect_timeout=60, retries={"max_attempts": 0}))
    print(f"runtime: {arn}  qualifier: {a.qualifier}\nsession: {sid}", file=sys.stderr)
    t0 = time.time()
    resp = c.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier=a.qualifier,
        runtimeSessionId=sid,
        payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = resp["response"].read().decode("utf-8")
    print(f"elapsed: {time.time() - t0:.1f}s", file=sys.stderr)

    if a.json:
        print(body)
        return 0
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return 1
    for k, v in d.items():
        if k != "response":
            print(f"{k}: {json.dumps(v, ensure_ascii=False)}", file=sys.stderr)
    print(d.get("response", ""))
    return 0 if d.get("exit_code", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
