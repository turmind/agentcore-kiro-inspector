#!/usr/bin/env python3
"""
Create the inspector runtime from scratch, then turn on MMDSv2.

Why two API calls: CreateAgentRuntime has no metadataConfiguration, and a
runtime without MMDSv2 rejects InvokeAgentRuntime with "This runtime is not
MMDSv2-enabled" (enforced since 2026-06-30, see the troubleshooting page). So:

  CreateAgentRuntime -> wait READY
  -> UpdateAgentRuntime (same full config + metadataConfiguration.requireMMDSV2)
  -> wait READY

Secrets come from THIS shell's environment (--set KEY), never from argv, and
are never printed -- only key names are. KIRO_API_KEY is always required.

  export KIRO_API_KEY=...                  # required
  export WECOM_WEBHOOK_KEY=...             # optional channel
  python3 scripts/create_rt.py --region <REGION> --image <URI>:<TAG> \\
      --role-arn arn:aws:iam::<ACCOUNT>:role/KiroInspectorBedrockAgentCoreRole \\
      --set WECOM_WEBHOOK_KEY --dry-run

  VPC mode (private subnets whose default route is a NAT gateway):
      --network VPC --subnet subnet-a --subnet subnet-b --security-group sg-x

In VPC mode the subnets / security groups are checked
read-only before anything is created (see check_vpc). A failed check stops the
run: no inbound rule by CIDR or prefix list, no public subnet.

--dry-run makes read-only calls only (sts, list runtimes, ec2 describe) and
validates both requests against the SDK model.

Output: progress on stderr, one JSON summary on stdout.
"""

import argparse
import copy
import json
import os
import re
import sys
import time
from functools import partial

import boto3
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

log = partial(print, file=sys.stderr)

NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")

# Values that must only ever come in through --set (i.e. from the environment).
SECRET_KEYS = {"KIRO_API_KEY", "WECOM_WEBHOOK_KEY", "WECOM_BOT_ID", "WECOM_BOT_SECRET",
               "LARK_APP_KEY"}
# Never put AWS credentials into the runtime: it runs as its execution role.
FORBIDDEN_KEYS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
MAX_ENV = 50

DEFAULT_DESCRIPTION = (
    "Kiro CLI headless inspector: RDS/Aurora, ElastiCache and other read-only "
    "maintenance checks; report pushed to WeCom / Feishu by the bridge"
)


# ----------------------------------------------------------------- builders

def build_env(region, set_keys, set_values, environ):
    """Return (env, errors). Secret values are read from `environ` only."""
    errors = []
    env = {"AWS_REGION": region, "AWS_DEFAULT_REGION": region}
    for kv in set_values:
        k, sep, v = kv.partition("=")
        if not sep or not k:
            errors.append(f"--set-value {kv!r}: expected KEY=VALUE")
        elif k in SECRET_KEYS:
            errors.append(f"--set-value {k}=...: secret, pass it with --set {k} (value from the environment)")
        else:
            env[k] = v
    for k in dict.fromkeys(["KIRO_API_KEY", *set_keys]):
        v = environ.get(k, "")
        if not v:
            errors.append(f"{k}: not set in this shell's environment")
        else:
            env[k] = v
    bad = sorted(FORBIDDEN_KEYS & set(env))
    if bad:
        errors.append(f"{bad}: AWS credentials must not be runtime env vars; the runtime uses its execution role")
    if len(env) > MAX_ENV:
        errors.append(f"{len(env)} env vars, API maximum is {MAX_ENV}")
    return env, errors


def build_network(mode, subnets, security_groups):
    errors = []
    if mode == "PUBLIC":
        if subnets or security_groups:
            errors.append("--subnet / --security-group only apply to --network VPC")
        return {"networkMode": "PUBLIC"}, errors
    for what, items in (("--subnet", subnets), ("--security-group", security_groups)):
        if not 1 <= len(items) <= 16:
            errors.append(f"{what}: VPC mode needs 1-16, got {len(items)}")
    # requireServiceS3Endpoint is deliberately absent: passing it to
    # CreateAgentRuntime returns ValidationException (API_VpcConfig).
    return {"networkMode": "VPC",
            "networkModeConfig": {"subnets": list(subnets), "securityGroups": list(security_groups)}}, errors


def build_create(name, image, role_arn, network, env, lifecycle, description):
    req = {
        "agentRuntimeName": name,
        "description": description,
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}},
        "roleArn": role_arn,
        "networkConfiguration": network,
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": lifecycle,
        "environmentVariables": env,
    }
    return req


def build_mmds_update(create_req, runtime_id):
    """UpdateAgentRuntime is a full replace: send back everything we created."""
    req = copy.deepcopy(create_req)
    req.pop("agentRuntimeName")
    req["agentRuntimeId"] = runtime_id
    req["metadataConfiguration"] = {"requireMMDSV2": True}
    return req


def validate(client, op, req):
    shape = client.meta.service_model.operation_model(op).input_shape
    report = ParamValidator().validate(req, shape)
    return report.generate_report() if report.has_errors() else ""


# ------------------------------------------------------- read-only checks

def _has_cidr(perm):
    return bool(perm.get("IpRanges") or perm.get("Ipv6Ranges") or perm.get("PrefixListIds"))


def _desc(perm):
    ports = "all" if perm.get("IpProtocol") == "-1" else f"{perm.get('IpProtocol')} {perm.get('FromPort')}-{perm.get('ToPort')}"
    src = [r["CidrIp"] for r in perm.get("IpRanges", [])] + \
          [r["CidrIpv6"] for r in perm.get("Ipv6Ranges", [])] + \
          [r["PrefixListId"] for r in perm.get("PrefixListIds", [])] + \
          [r["GroupId"] for r in perm.get("UserIdGroupPairs", [])]
    return f"{ports} <- {src}"


def check_vpc(session, subnets, security_groups):
    """Return (errors, warnings, facts). Describe calls only."""
    ec2 = session.client("ec2")
    errs, warns, facts = [], [], {}

    sub = ec2.describe_subnets(SubnetIds=subnets)["Subnets"]
    vpcs = sorted({s["VpcId"] for s in sub})
    if len(vpcs) != 1:
        errs.append(f"subnets span several VPCs: {vpcs}")
    vpc = vpcs[0]
    az_of = {s["SubnetId"]: s["AvailabilityZoneId"] for s in sub}
    facts.update(vpc=vpc, subnet_az_ids=az_of)
    if len(set(az_of.values())) < 2:
        warns.append("subnets are in a single AZ; official best practice is >= 2 private subnets in different AZs")

    for attr, key in (("enableDnsSupport", "EnableDnsSupport"), ("enableDnsHostnames", "EnableDnsHostnames")):
        if not ec2.describe_vpc_attribute(VpcId=vpc, Attribute=attr)[key]["Value"]:
            errs.append(f"{vpc}: {attr} is off (required)")

    rts = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["RouteTables"]
    main = next((r for r in rts if any(a.get("Main") for a in r.get("Associations", []))), None)
    for sn in subnets:
        rt = next((r for r in rts if any(a.get("SubnetId") == sn for a in r.get("Associations", []))), main)
        routes = rt.get("Routes", []) if rt else []
        if any(r.get("GatewayId", "").startswith("igw-") for r in routes):
            errs.append(f"{sn}: route table {rt['RouteTableId']} has an internet-gateway route (public subnet). "
                        "Use private subnets whose 0.0.0.0/0 goes to a NAT gateway")
        elif not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("NatGatewayId") for r in routes):
            warns.append(f"{sn}: no 0.0.0.0/0 -> NAT gateway route; the Kiro API and WeCom/Feishu are unreachable")

    groups = ec2.describe_security_groups(GroupIds=security_groups)["SecurityGroups"]
    for g in groups:
        gid = g["GroupId"]
        if g["VpcId"] != vpc:
            errs.append(f"{gid}: in {g['VpcId']}, subnets are in {vpc}")
        for p in g.get("IpPermissions", []):
            if _has_cidr(p):
                errs.append(f"{gid}: inbound rule by CIDR / prefix list ({_desc(p)}). The runtime needs no inbound rule")
            elif p.get("UserIdGroupPairs"):
                errs.append(f"{gid}: inbound rule {_desc(p)}. The runtime needs no inbound rule")
        for p in g.get("IpPermissionsEgress", []):
            if p.get("IpProtocol") == "-1" and any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", [])):
                warns.append(f"{gid}: egress all traffic to 0.0.0.0/0; least privilege is TCP 443 out")

    return errs, warns, facts


# ------------------------------------------------------------------- main

def wait_ready(c, runtime_id, timeout_s):
    deadline = time.time() + timeout_s
    while True:
        r = c.get_agent_runtime(agentRuntimeId=runtime_id)
        st = r["status"]
        if st == "READY":
            return r
        if st.endswith("_FAILED"):
            raise SystemExit(f"{runtime_id}: {st}: {r.get('failureReason', '(no failureReason)')}")
        if time.time() > deadline:
            raise SystemExit(f"{runtime_id}: still {st} after {timeout_s}s")
        time.sleep(5)


def find_by_name(c, name):
    token = None
    while True:
        kw = {"nextToken": token} if token else {}
        page = c.list_agent_runtimes(**kw)
        for r in page.get("agentRuntimes", []):
            if r.get("agentRuntimeName") == name:
                return r
        token = page.get("nextToken")
        if not token:
            return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--region", default=os.environ.get("AWS_REGION"), help="required (or AWS_REGION)")
    p.add_argument("--name", default="kiro_inspector", help="agentRuntimeName, [a-zA-Z][a-zA-Z0-9_]{0,47}")
    p.add_argument("--image", help="full containerUri, e.g. <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/<REPO>:<TAG>")
    p.add_argument("--repo", default="bedrock-agentcore-kiro-inspector", help="with --tag, instead of --image")
    p.add_argument("--tag", help="image tag in --repo of this account/region")
    p.add_argument("--role-arn", help="execution role ARN (default: role --role-name in this account)")
    p.add_argument("--role-name", default="KiroInspectorBedrockAgentCoreRole")
    p.add_argument("--network", choices=("PUBLIC", "VPC"), default="PUBLIC")
    p.add_argument("--subnet", action="append", default=[])
    p.add_argument("--security-group", action="append", default=[])
    p.add_argument("--idle-timeout", type=int, default=60, help="idleRuntimeSessionTimeout, 60-28800")
    p.add_argument("--max-lifetime", type=int, default=28800, help="maxLifetime, 60-28800")
    p.add_argument("--description", default=DEFAULT_DESCRIPTION)
    p.add_argument("--set", action="append", default=[], metavar="KEY",
                   help="copy KEY from this shell's environment into the runtime (repeatable)")
    p.add_argument("--set-value", action="append", default=[], metavar="KEY=VALUE",
                   help="non-secret KEY=VALUE (repeatable)")
    p.add_argument("--skip-network-checks", action="store_true",
                   help="do not run the read-only VPC checks (only if the caller lacks ec2 Describe)")
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait for each READY")
    p.add_argument("--dry-run", action="store_true", help="read-only; validate and print, create nothing")
    a = p.parse_args()

    errors = []
    if not a.region:
        p.error("--region is required (or set AWS_REGION)")
    if not NAME_RE.match(a.name):
        errors.append(f"--name {a.name!r}: must match [a-zA-Z][a-zA-Z0-9_]{{0,47}}")
    if bool(a.image) == bool(a.tag):
        errors.append("give exactly one of --image or --tag")
    for what, v in (("--idle-timeout", a.idle_timeout), ("--max-lifetime", a.max_lifetime)):
        if not 60 <= v <= 28800:
            errors.append(f"{what} {v}: must be 60-28800")
    if a.idle_timeout > a.max_lifetime:
        errors.append("--idle-timeout is larger than --max-lifetime")

    network, e = build_network(a.network, a.subnet, a.security_group)
    errors += e
    env, e = build_env(a.region, a.set, a.set_value, os.environ)
    errors += e
    if errors:
        for x in errors:
            log("error:", x)
        return 2

    s = boto3.Session(region_name=a.region)
    account = s.client("sts").get_caller_identity()["Account"]
    image = a.image or f"{account}.dkr.ecr.{a.region}.amazonaws.com/{a.repo}:{a.tag}"
    role_arn = a.role_arn or f"arn:aws:iam::{account}:role/{a.role_name}"
    if "BedrockAgentCore" not in role_arn.rsplit("/", 1)[-1]:
        log(f"note: role name has no 'BedrockAgentCore' -> the PassRole grant in BedrockAgentCoreFullAccess "
            f"(role/*BedrockAgentCore*) does not cover it; the caller needs its own iam:PassRole on {role_arn}")

    facts = {}
    if a.network == "VPC" and not a.skip_network_checks:
        errs, warns, facts = check_vpc(s, a.subnet, a.security_group)
        for w in warns:
            log("warning:", w)
        if errs:
            for x in errs:
                log("error:", x)
            return 2

    lifecycle = {"idleRuntimeSessionTimeout": a.idle_timeout, "maxLifetime": a.max_lifetime}
    create_req = build_create(a.name, image, role_arn, network, env, lifecycle, a.description)
    c = s.client("bedrock-agentcore-control")
    for op, req in (("CreateAgentRuntime", create_req),
                    ("UpdateAgentRuntime", build_mmds_update(create_req, "placeholder-0123456789"))):
        problem = validate(c, op, req)
        if problem:
            log(f"{op} request does not match the SDK model:\n{problem}")
            return 2

    existing = find_by_name(c, a.name)
    if existing:
        log(f"runtime named {a.name} already exists ({existing['agentRuntimeId']}); "
            "use scripts/update_rt.py to change it")
        return 2

    summary = {
        "dry_run": a.dry_run,
        "name": a.name,
        "image": image,
        "role": role_arn,
        "network": network,
        "lifecycle": lifecycle,
        "env_keys": sorted(env),
        "checks": facts,
    }
    if a.dry_run:
        print(json.dumps(summary, indent=1, ensure_ascii=False))
        return 0

    r = c.create_agent_runtime(**create_req)
    rid = r["agentRuntimeId"]
    log(f"created {rid} (version {r.get('agentRuntimeVersion')}), waiting for READY")
    wait_ready(c, rid, a.timeout)

    log("enabling MMDSv2 (UpdateAgentRuntime with the full config)")
    try:
        c.update_agent_runtime(**build_mmds_update(create_req, rid))
    except ClientError as ex:
        log(f"MMDSv2 update failed: {ex}\n"
            f"retry with: python3 scripts/update_rt.py --region {a.region} --runtime-id {rid} --mmdsv2 --wait")
        return 1
    final = wait_ready(c, rid, a.timeout)

    summary.update(
        runtime_id=rid,
        runtime_arn=final["agentRuntimeArn"],
        version=final.get("agentRuntimeVersion"),
        status=final["status"],
        mmdsv2=(final.get("metadataConfiguration") or {}).get("requireMMDSV2"),
    )
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    log("READY does not mean the new container serves yet: wait ~60s before the first invoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
