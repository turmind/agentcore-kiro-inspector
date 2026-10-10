#!/usr/bin/env python3
"""
Daily EventBridge Scheduler job for the inspector runtime.

Scheduler calls InvokeAgentRuntime directly through the universal target
arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime (verified end
to end, see the deploy doc). No Lambda in between.

It sends {"prompt": ..., "async": true, "state_mode": "ephemeral"}: the runtime
answers in seconds and runs + pushes the report in the background.

What it creates (idempotent, re-run to update):
  schedule group   kiro-inspector
  scheduler role   KiroInspectorSchedulerRole   (iam/scheduler-*.json)
  schedule         kiro-inspector-daily         cron(0 9 * * ? *) Asia/Shanghai

  R="--region <REGION> --runtime-id <RUNTIME_ID>"
  python3 scripts/create_schedule.py $R --dry-run         # read-only, prints requests
  python3 scripts/create_schedule.py $R                   # create / update
  python3 scripts/create_schedule.py $R --test-in 3       # + one-shot run in 3 min
  python3 scripts/create_schedule.py --region <REGION> --delete

--delete removes the daily schedule. It deliberately leaves the schedule group
and the IAM role; the deploy doc's uninstall section removes those.

Nothing here opens a network path into the account: the runtime is reached
with SigV4 only.
"""

import argparse
import json
import os
import sys
import time
from functools import partial
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

GROUP = "kiro-inspector"
SCHEDULE = "kiro-inspector-daily"
SCHED_ROLE = "KiroInspectorSchedulerRole"
log = partial(print, file=sys.stderr)  # progress -> stderr, result JSON -> stdout

UNIVERSAL_ARN = "arn:aws:scheduler:::aws-sdk:bedrockagentcore:invokeAgentRuntime"

# 29 chars + execution id (16 chars in the docs' example) -> 45, above the
# 33-char minimum of runtimeSessionId. A fresh id per run = fresh microVM.
SESSION_PREFIX = "kiro-inspector-scheduled-run-"

DEFAULT_PROMPT = (
    "巡检 {region} 区域：1) RDS/Aurora 所有实例与集群的待维护动作（pending maintenance actions），"
    "2) ElastiCache 未完成的服务更新。把结果整理成简明中文报告。"
)


def fill(path: str, **kv) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        text = f.read()
    for k, v in kv.items():
        text = text.replace(f"<{k}>", v)
    return text


def runtime_payload(prompt: str) -> dict:
    return {"prompt": prompt, "async": True, "state_mode": "ephemeral"}


def universal_input(runtime_arn: str, prompt: str, camel: bool) -> str:
    # Scheduler's own examples write parameters PascalCase even where the
    # service API is camelCase (sfn: Input / StateMachineArn). Header and URI
    # members (runtimeSessionId, agentRuntimeArn) go in the same JSON.
    names = ["AgentRuntimeArn", "RuntimeSessionId", "ContentType", "Accept", "Payload"]
    if camel:
        names = [n[0].lower() + n[1:] for n in names]
    values = [
        runtime_arn,
        SESSION_PREFIX + "<aws.scheduler.execution-id>",
        "application/json",
        "application/json",
        json.dumps(runtime_payload(prompt), ensure_ascii=False),  # blob -> JSON string
    ]
    return json.dumps(dict(zip(names, values)), ensure_ascii=False)


def check_universal_names(input_json: str) -> list:
    """Each Input key must be a member of InvokeAgentRuntime (case-insensitive)."""
    shape = boto3.client("bedrock-agentcore", region_name="us-east-1").meta.service_model \
        .operation_model("InvokeAgentRuntime").input_shape
    members = {m.lower(): m for m in shape.members}
    bad = [k for k in json.loads(input_json) if k.lower() not in members]
    missing = [m for m in shape.required_members
               if m.lower() not in {k.lower() for k in json.loads(input_json)}]
    return bad + [f"missing required {m}" for m in missing]


def ensure_role(iam, name: str, trust: str, policy_name: str, policy: str,
                dry: bool = False) -> str:
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        if not dry:
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        if dry:
            return f"(would create role {name})"
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust)["Role"]["Arn"]
        time.sleep(10)  # new roles take a few seconds before a service can assume them
    if not dry:
        iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=policy)
    return arn


def ensure_group(sch, dry: bool) -> None:
    try:
        sch.get_schedule_group(Name=GROUP)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        log(f"schedule group {GROUP}: {'would create' if dry else 'creating'}")
        if not dry:
            sch.create_schedule_group(Name=GROUP)


def upsert_schedule(sch, req: dict, dry: bool) -> None:
    report = ParamValidator().validate(
        req, sch.meta.service_model.operation_model("CreateSchedule").input_shape)
    if report.has_errors():
        raise SystemExit(report.generate_report())
    if dry:
        return
    try:
        sch.get_schedule(Name=req["Name"], GroupName=GROUP)
        sch.update_schedule(**req)  # full replace, same shape as create
        log(f"schedule {req['Name']}: updated")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        sch.create_schedule(**req)
        log(f"schedule {req['Name']}: created")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--region", default=os.environ.get("AWS_REGION"), help="required (or AWS_REGION)")
    p.add_argument("--runtime-id", default=os.environ.get("RUNTIME_ID"),
                   help="required except with --delete (or RUNTIME_ID)")
    p.add_argument("--param-case", choices=("pascal", "camel"), default="pascal",
                   help="universal target Input key style (pascal = Scheduler doc convention)")
    p.add_argument("--cron", default="cron(0 9 * * ? *)")
    p.add_argument("--timezone", default="Asia/Shanghai")
    p.add_argument("--prompt", help="default: RDS/Aurora + ElastiCache maintenance of --region")
    p.add_argument("--prompt-file")
    p.add_argument("--disabled", action="store_true", help="create the schedule in DISABLED state")
    p.add_argument("--test-in", type=int, metavar="MIN",
                   help="also create a one-shot schedule MIN minutes from now (auto-deleted after it runs)")
    p.add_argument("--delete", action="store_true",
                   help="remove the daily schedule; leaves group kiro-inspector and role KiroInspectorSchedulerRole")
    p.add_argument("--dry-run", action="store_true", help="read-only: print what would be sent")
    a = p.parse_args()
    if not a.region:
        p.error("--region is required (or set AWS_REGION)")
    if not a.runtime_id and not a.delete:
        p.error("--runtime-id is required (or set RUNTIME_ID)")

    s = boto3.Session(region_name=a.region)
    sch, iam = s.client("scheduler"), s.client("iam")
    account = s.client("sts").get_caller_identity()["Account"]

    if a.delete:
        for name in (SCHEDULE,):
            try:
                sch.delete_schedule(Name=name, GroupName=GROUP)
                log(f"schedule {name}: deleted")
            except ClientError as e:
                log(f"schedule {name}: {e.response['Error']['Code']}")
        log(f"left in place: group {GROUP}, role {SCHED_ROLE}")
        return 0

    runtime_arn = s.client("bedrock-agentcore-control").get_agent_runtime(
        agentRuntimeId=a.runtime_id)["agentRuntimeArn"]
    if a.prompt_file:
        with open(a.prompt_file, encoding="utf-8") as f:
            prompt = f.read().strip()
    else:
        prompt = a.prompt or DEFAULT_PROMPT.format(region=a.region)

    ph = {"REGION": a.region, "ACCOUNT": account, "RUNTIME_ID": a.runtime_id}
    trust = fill("iam/scheduler-trust-policy.json", **ph)

    target_arn = UNIVERSAL_ARN
    target_input = universal_input(runtime_arn, prompt, a.param_case == "camel")
    problems = check_universal_names(target_input)
    if problems:
        raise SystemExit(f"Input does not match InvokeAgentRuntime: {problems}")
    role_arn = ensure_role(iam, SCHED_ROLE, trust, "invoke-agent-runtime",
                           fill("iam/scheduler-invoke-policy.json", **ph), dry=a.dry_run)

    ensure_group(sch, a.dry_run)

    target = {
        "Arn": target_arn,
        "RoleArn": role_arn,
        "Input": target_input,
        # One attempt only: a retry would be a second full inspection (and a
        # second report). A missed day shows up in TargetErrorCount instead.
        "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 3600},
    }
    reqs = [{
        "Name": SCHEDULE,
        "GroupName": GROUP,
        "ScheduleExpression": a.cron,
        "ScheduleExpressionTimezone": a.timezone,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "State": "DISABLED" if a.disabled else "ENABLED",
        "Description": f"Daily Kiro inspection -> {a.runtime_id}",
        "Target": target,
    }]
    if a.test_in:
        at = datetime.now(timezone.utc) + timedelta(minutes=a.test_in)
        reqs.append({
            "Name": f"kiro-inspector-test-{at:%Y%m%d%H%M}",
            "GroupName": GROUP,
            "ScheduleExpression": f"at({at:%Y-%m-%dT%H:%M:00})",
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "ActionAfterCompletion": "DELETE",
            "Target": target,
        })

    for r in reqs:
        upsert_schedule(sch, r, a.dry_run)

    shown = json.loads(json.dumps(reqs))
    for r in shown:
        r["Target"]["Input"] = json.loads(r["Target"]["Input"])
    print(json.dumps({"dry_run": a.dry_run, "requests": shown},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
