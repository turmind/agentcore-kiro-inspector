#!/usr/bin/env python3
"""
Roll an existing runtime to a new image, change its environment variables,
and/or turn on MMDSv2 -- without dropping anything else that is configured.

UpdateAgentRuntime is a full replace, not a patch: any optional block you leave
out goes back to its default. So this script reads the current runtime and
passes every block back (lifecycle, metadataConfiguration, filesystem
configurations, protocol, network, env), changing only what you asked for.

Secret values are read from THIS process's environment, never from argv, and
are never printed -- only key names are.

  # new image, WeCom on (value comes from the env var of the same name)
  export WECOM_WEBHOOK_KEY=...
  python3 scripts/update_rt.py --region <REGION> --runtime-id <RUNTIME_ID> \\
      --image <URI>:<TAG> --set WECOM_WEBHOOK_KEY --wait

  # Feishu off without deleting its credentials / fully remove WeCom
  python3 scripts/update_rt.py ... --set-value LARK_CHANNEL=OFF
  python3 scripts/update_rt.py ... --unset WECOM_WEBHOOK_KEY

  # idle session released after 60s (async jobs stay alive via HealthyBusy)
  python3 scripts/update_rt.py ... --idle-timeout 60 --wait

  # a runtime created without MMDSv2 cannot be invoked (since 2026-06-30)
  python3 scripts/update_rt.py ... --mmdsv2 --wait

  --dry-run   read-only: validate the request, print image / env keys, change nothing

Known risk (not verified live): GetAgentRuntime can return
networkModeConfig.requireServiceS3Endpoint, and API_VpcConfig says runtimes
created after the 2026-05-05 rollout reject that field on Update. The request
is sent as read first; only if the API answers ValidationException naming that
field is it removed and the call retried once. It is not stripped up front,
because for a runtime created before the rollout "absent" means "true" and
would silently re-attach the service-managed S3 gateway.

Output: progress on stderr, JSON on stdout.
"""

import argparse
import copy
import json
import os
import sys
import time
from functools import partial

import boto3
from botocore.exceptions import ClientError

from create_rt import FORBIDDEN_KEYS, MAX_ENV, SECRET_KEYS, validate

log = partial(print, file=sys.stderr)

OPTIONAL_BLOCKS = (
    "description",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "metadataConfiguration",
    "filesystemConfigurations",
    "requestHeaderConfiguration",
    "authorizerConfiguration",
)
S3_FLAG = "requireServiceS3Endpoint"


def apply_env(cur_env, set_keys, set_values, unset_keys, environ):
    """Return (env, errors). Secret values come from `environ` only."""
    env, errors = dict(cur_env or {}), []
    for kv in set_values:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            errors.append(f"--set-value {kv!r}: expected KEY=VALUE")
        elif k in SECRET_KEYS:
            errors.append(f"--set-value {k}=...: secret, pass it with --set {k} (value from the environment)")
        else:
            env[k] = v
    for k in set_keys:
        v = environ.get(k, "")
        if not v:
            errors.append(f"--set {k}: not set in this shell's environment")
        else:
            env[k] = v
    for k in unset_keys:
        env.pop(k, None)
    bad = sorted(FORBIDDEN_KEYS & set(env))
    if bad:
        errors.append(f"{bad}: AWS credentials must not be runtime env vars; the runtime uses its execution role")
    if not env.get("KIRO_API_KEY"):
        errors.append("KIRO_API_KEY would be missing; the bridge cannot start Kiro without it")
    if len(env) > MAX_ENV:
        errors.append(f"{len(env)} env vars, API maximum is {MAX_ENV}")
    return env, errors


def build_update(cur, runtime_id, env, image=None, mmdsv2=False, idle=None, max_life=None):
    artifact = cur["agentRuntimeArtifact"]
    if image:
        artifact = {"containerConfiguration": {"containerUri": image}}
    req = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": artifact,
        "roleArn": cur["roleArn"],
        "networkConfiguration": copy.deepcopy(cur["networkConfiguration"]),
        "environmentVariables": env,
    }
    for k in OPTIONAL_BLOCKS:
        if cur.get(k):
            req[k] = copy.deepcopy(cur[k])
    if mmdsv2:
        req["metadataConfiguration"] = {**req.get("metadataConfiguration", {}), "requireMMDSV2": True}
    if idle is not None or max_life is not None:
        life = dict(req.get("lifecycleConfiguration") or {})
        if idle is not None:
            life["idleRuntimeSessionTimeout"] = idle
        if max_life is not None:
            life["maxLifetime"] = max_life
        req["lifecycleConfiguration"] = life
    return req


def is_s3_flag_rejection(err):
    e = err.response.get("Error", {})
    return e.get("Code") == "ValidationException" and S3_FLAG in (e.get("Message") or "")


def strip_s3_flag(req):
    """Return a copy without networkModeConfig.requireServiceS3Endpoint, or None if absent."""
    nmc = (req.get("networkConfiguration") or {}).get("networkModeConfig") or {}
    if S3_FLAG not in nmc:
        return None
    out = copy.deepcopy(req)
    out["networkConfiguration"]["networkModeConfig"].pop(S3_FLAG)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--region", default=os.environ.get("AWS_REGION"), help="required (or AWS_REGION)")
    p.add_argument("--runtime-id", default=os.environ.get("RUNTIME_ID"), help="required (or RUNTIME_ID)")
    p.add_argument("--image", help="new containerUri; default: keep current")
    p.add_argument("--set", action="append", default=[], metavar="KEY",
                   help="copy KEY from this shell's environment into the runtime (repeatable)")
    p.add_argument("--set-value", action="append", default=[], metavar="KEY=VALUE",
                   help="non-secret KEY=VALUE (repeatable)")
    p.add_argument("--unset", action="append", default=[], metavar="KEY")
    p.add_argument("--mmdsv2", action="store_true", help="set metadataConfiguration.requireMMDSV2=true")
    p.add_argument("--idle-timeout", type=int, help="lifecycleConfiguration.idleRuntimeSessionTimeout, 60-28800")
    p.add_argument("--max-lifetime", type=int, help="lifecycleConfiguration.maxLifetime, 60-28800")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--wait", action="store_true", help="poll until READY (60 x 5s)")
    a = p.parse_args()
    if not a.region:
        p.error("--region is required (or set AWS_REGION)")
    if not a.runtime_id:
        p.error("--runtime-id is required (or set RUNTIME_ID)")

    c = boto3.client("bedrock-agentcore-control", region_name=a.region)
    cur = c.get_agent_runtime(agentRuntimeId=a.runtime_id)

    env, errors = apply_env(cur.get("environmentVariables"), a.set, a.set_value, a.unset, os.environ)
    for what, v in (("--idle-timeout", a.idle_timeout), ("--max-lifetime", a.max_lifetime)):
        if v is not None and not 60 <= v <= 28800:
            errors.append(f"{what} {v}: must be 60-28800")
    if errors:
        for x in errors:
            log("error:", x)
        return 2
    req = build_update(cur, a.runtime_id, env, a.image, a.mmdsv2, a.idle_timeout, a.max_lifetime)
    problem = validate(c, "UpdateAgentRuntime", req)
    if problem:
        log(f"UpdateAgentRuntime request does not match the SDK model:\n{problem}")
        return 2

    nmc = req["networkConfiguration"].get("networkModeConfig") or {}
    summary = {
        "dry_run": a.dry_run,
        "runtime": a.runtime_id,
        "version_before": cur.get("agentRuntimeVersion"),
        "image": req["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"],
        "network_mode": req["networkConfiguration"].get("networkMode"),
        S3_FLAG + "_as_read": nmc.get(S3_FLAG, "(absent)"),
        "mmdsv2": (req.get("metadataConfiguration") or {}).get("requireMMDSV2", False),
        "lifecycle": req.get("lifecycleConfiguration"),
        "env_keys": sorted(env),
        "kept": [k for k in OPTIONAL_BLOCKS if k in req],
    }
    if a.dry_run:
        print(json.dumps(summary, indent=1))
        return 0
    if not summary["mmdsv2"]:
        log("warning: MMDSv2 is off on this runtime; invokes are rejected until you add --mmdsv2")

    try:
        r = c.update_agent_runtime(**req)
    except ClientError as ex:
        retry = strip_s3_flag(req) if is_s3_flag_rejection(ex) else None
        if retry is None:
            raise
        log(f"API rejected {S3_FLAG} on update ({ex.response['Error']['Message']}); "
            "retrying once without it (runtime created after the 2026-05-05 rollout)")
        r = c.update_agent_runtime(**retry)
        summary["retried_without_" + S3_FLAG] = True
    summary.update(version_after=r.get("agentRuntimeVersion"), status=r.get("status"))

    if a.wait:
        for _ in range(60):
            g = c.get_agent_runtime(agentRuntimeId=a.runtime_id)
            st = g["status"]
            if st == "READY" or st.endswith("_FAILED"):
                summary["status"] = st
                if st.endswith("_FAILED"):
                    summary["failureReason"] = g.get("failureReason")
                break
            time.sleep(5)
        # READY comes back quickly, but the previous container can keep serving
        # for a while. Measured: wait 45-60s before testing the new behaviour.
        log("READY does not mean the new container serves yet: wait ~60s before invoking")
    print(json.dumps(summary, indent=1))
    return 1 if str(summary.get("status", "")).endswith("_FAILED") else 0


if __name__ == "__main__":
    sys.exit(main())
