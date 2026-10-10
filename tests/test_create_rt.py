"""
Offline tests for scripts/create_rt.py and scripts/update_rt.py request
builders. No AWS call: requests are validated against the installed botocore
model only.

  python3 -m unittest tests/test_create_rt.py -v
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import create_rt as cr  # noqa: E402
import update_rt as ur  # noqa: E402

CTRL = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
IMG = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/bedrock-agentcore-kiro-inspector:v5"
ROLE = "arn:aws:iam::123456789012:role/KiroInspectorBedrockAgentCoreRole"
LIFE = {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}
ENVIRON = {"KIRO_API_KEY": "fake-kiro", "WECOM_WEBHOOK_KEY": "fake-wecom"}


def create_req(mode="PUBLIC"):
    net, e1 = cr.build_network(mode, ["subnet-a", "subnet-b"] if mode == "VPC" else [],
                               ["sg-a"] if mode == "VPC" else [])
    env, e2 = cr.build_env("ap-northeast-1", ["WECOM_WEBHOOK_KEY"], [], ENVIRON)
    assert not (e1 or e2), (e1, e2)
    return cr.build_create("kiro_inspector_t", IMG, ROLE, net, env, LIFE, cr.DEFAULT_DESCRIPTION)


class Env(unittest.TestCase):
    def test_kiro_api_key_always_required(self):
        _, errs = cr.build_env("r", [], [], {})
        self.assertTrue(any("KIRO_API_KEY" in e for e in errs))

    def test_secret_via_set_value_rejected(self):
        for k in sorted(cr.SECRET_KEYS):
            _, errs = cr.build_env("r", [], [f"{k}=x"], ENVIRON)
            self.assertTrue(any(k in e and "--set" in e for e in errs), k)

    def test_aws_credentials_rejected(self):
        for k in sorted(cr.FORBIDDEN_KEYS):
            _, errs = cr.build_env("r", [], [f"{k}=x"], ENVIRON)
            self.assertTrue(any("AWS credentials" in e for e in errs), k)
            _, errs = cr.build_env("r", [k], [], {**ENVIRON, k: "x"})
            self.assertTrue(any("AWS credentials" in e for e in errs), k)

    def test_set_reads_environment_only(self):
        env, errs = cr.build_env("r", ["WECOM_WEBHOOK_KEY"], ["KIRO_MODEL=m"], ENVIRON)
        self.assertEqual(errs, [])
        self.assertEqual(env["WECOM_WEBHOOK_KEY"], "fake-wecom")
        self.assertEqual(env["KIRO_MODEL"], "m")
        self.assertEqual(env["AWS_REGION"], "r")
        self.assertNotIn("LARK_CHANNEL", env)

    def test_env_limit(self):
        many = [f"K{i}=v" for i in range(60)]
        _, errs = cr.build_env("r", [], many, ENVIRON)
        self.assertTrue(any("maximum" in e for e in errs))


class Network(unittest.TestCase):
    def test_no_require_service_s3_endpoint(self):
        for mode in ("PUBLIC", "VPC"):
            net, _ = cr.build_network(mode, ["subnet-a"] if mode == "VPC" else [],
                                      ["sg-a"] if mode == "VPC" else [])
            self.assertNotIn("requireServiceS3Endpoint", str(net), mode)

    def test_vpc_needs_subnets_and_sgs(self):
        _, errs = cr.build_network("VPC", [], [])
        self.assertEqual(len(errs), 2)
        _, errs = cr.build_network("PUBLIC", ["subnet-a"], [])
        self.assertTrue(errs)


class Requests(unittest.TestCase):
    def test_create_and_mmds_update_validate(self):
        for mode in ("PUBLIC", "VPC"):
            req = create_req(mode)
            self.assertEqual(cr.validate(CTRL, "CreateAgentRuntime", req), "", mode)
            upd = cr.build_mmds_update(req, "placeholder-0123456789")
            self.assertEqual(cr.validate(CTRL, "UpdateAgentRuntime", upd), "", mode)
            self.assertNotIn("filesystemConfigurations", req)

    def test_create_has_no_metadata_configuration(self):
        # CreateAgentRuntime has no such member in the SDK model: MMDSv2 must be
        # a follow-up UpdateAgentRuntime.
        members = CTRL.meta.service_model.operation_model("CreateAgentRuntime").input_shape.members
        self.assertNotIn("metadataConfiguration", members)
        self.assertNotIn("metadataConfiguration", create_req())

    def test_mmds_update_is_full_replace(self):
        req = create_req("VPC")
        upd = cr.build_mmds_update(req, "rt-1234567890")
        self.assertNotIn("agentRuntimeName", upd)
        self.assertEqual(upd["agentRuntimeId"], "rt-1234567890")
        self.assertEqual(upd["metadataConfiguration"], {"requireMMDSV2": True})
        for k in req:
            if k != "agentRuntimeName":
                self.assertEqual(upd[k], req[k], k)
        self.assertNotIn("metadataConfiguration", req)  # deep copy, original untouched


def current(network=None):
    return {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMG}},
        "roleArn": ROLE,
        "networkConfiguration": network or {"networkMode": "PUBLIC"},
        "environmentVariables": {"KIRO_API_KEY": "k", "LARK_CHANNEL": "ON"},
        "lifecycleConfiguration": LIFE,
        "protocolConfiguration": {"serverProtocol": "HTTP"},
    }


class Update(unittest.TestCase):
    def test_mmdsv2_flag(self):
        req = ur.build_update(current(), "rt-1234567890", {"KIRO_API_KEY": "k"}, mmdsv2=True)
        self.assertEqual(req["metadataConfiguration"], {"requireMMDSV2": True})
        self.assertEqual(cr.validate(CTRL, "UpdateAgentRuntime", req), "")
        req = ur.build_update(current(), "rt-1234567890", {"KIRO_API_KEY": "k"})
        self.assertNotIn("metadataConfiguration", req)

    def test_env_rules_match_create(self):
        _, errs = ur.apply_env({"KIRO_API_KEY": "k"}, [], ["KIRO_API_KEY=x"], [], {})
        self.assertTrue(errs)
        _, errs = ur.apply_env({"KIRO_API_KEY": "k"}, [], ["AWS_ACCESS_KEY_ID=x"], [], {})
        self.assertTrue(errs)
        _, errs = ur.apply_env({"KIRO_API_KEY": "k"}, [], [], ["KIRO_API_KEY"], {})
        self.assertTrue(any("KIRO_API_KEY" in e for e in errs))
        env, errs = ur.apply_env({"KIRO_API_KEY": "k", "LARK_CHANNEL": "ON"}, [], [], ["LARK_CHANNEL"], {})
        self.assertEqual((env, errs), ({"KIRO_API_KEY": "k"}, []))

    def test_s3_flag_kept_as_read_and_strippable(self):
        net = {"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-a"], "securityGroups": ["sg-a"], "requireServiceS3Endpoint": False}}
        req = ur.build_update(current(net), "rt-1234567890", {"KIRO_API_KEY": "k"})
        self.assertIs(req["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"], False)
        retry = ur.strip_s3_flag(req)
        self.assertNotIn("requireServiceS3Endpoint", retry["networkConfiguration"]["networkModeConfig"])
        self.assertIn("requireServiceS3Endpoint", req["networkConfiguration"]["networkModeConfig"])
        self.assertIsNone(ur.strip_s3_flag(ur.build_update(current(), "rt-1234567890", {"KIRO_API_KEY": "k"})))

    def test_s3_flag_rejection_detection(self):
        def err(code, msg):
            return ClientError({"Error": {"Code": code, "Message": msg}}, "UpdateAgentRuntime")
        self.assertTrue(ur.is_s3_flag_rejection(err("ValidationException", "requireServiceS3Endpoint is not supported")))
        self.assertFalse(ur.is_s3_flag_rejection(err("ValidationException", "roleArn invalid")))
        self.assertFalse(ur.is_s3_flag_rejection(err("AccessDeniedException", "requireServiceS3Endpoint")))


if __name__ == "__main__":
    unittest.main()
