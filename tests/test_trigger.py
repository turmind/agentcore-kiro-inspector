"""
Offline tests for the scheduler side: the request builders in
scripts/create_schedule.py. No AWS calls.

  python3 tests/test_trigger.py -v
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "scripts")]

import boto3  # noqa: E402
from botocore.validate import ParamValidator  # noqa: E402

import create_schedule as cs  # noqa: E402

ARN = "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/kiro_inspector-abc"
# What <aws.scheduler.execution-id> looks like in the Scheduler docs.
EXEC_ID = "d32c5kddcf5bb8c3"


def invoke_shape():
    return boto3.client("bedrock-agentcore", region_name="us-east-1").meta.service_model \
        .operation_model("InvokeAgentRuntime").input_shape


class ScheduleInput(unittest.TestCase):
    def test_session_prefix_length(self):
        # 33 is the runtimeSessionId minimum; the prefix alone must already be
        # close enough that any non-empty execution id clears it.
        self.assertGreaterEqual(len(cs.SESSION_PREFIX + EXEC_ID), 33)
        self.assertGreaterEqual(len(cs.SESSION_PREFIX), 29)

    def test_universal_keys_match_api_both_cases(self):
        for camel in (False, True):
            inp = cs.universal_input(ARN, "巡检", camel)
            self.assertEqual(cs.check_universal_names(inp), [])

    def test_universal_payload_is_json_string(self):
        inp = json.loads(cs.universal_input(ARN, "巡检", False))
        self.assertIsInstance(inp["Payload"], str)
        self.assertEqual(json.loads(inp["Payload"])["async"], True)

    def test_universal_after_substitution_is_valid(self):
        # Scheduler replaces the placeholder, then calls the API: the result,
        # mapped onto botocore names, must pass the real input shape.
        inp = json.loads(cs.universal_input(ARN, "巡检", True)
                         .replace("<aws.scheduler.execution-id>", EXEC_ID))
        inp["payload"] = inp["payload"].encode()
        report = ParamValidator().validate(inp, invoke_shape())
        self.assertFalse(report.has_errors(), report.generate_report())

    def test_policies_have_no_placeholders_left(self):
        ph = {"REGION": "ap-northeast-1", "ACCOUNT": "123456789012", "RUNTIME_ID": "rt"}
        files = sorted(f"iam/{n}" for n in os.listdir(os.path.join(ROOT, "iam")) if n.endswith(".json"))
        self.assertEqual(files, ["iam/deployer-policy.json", "iam/runtime-execution-policy.json",
                                 "iam/runtime-trust-policy.json", "iam/scheduler-invoke-policy.json",
                                 "iam/scheduler-trust-policy.json"])
        for f in files:
            text = cs.fill(f, **ph)
            self.assertNotIn("<", text, f)
            doc = json.loads(text)
            # IAM rejects unknown top-level keys such as "_comment".
            self.assertLessEqual(set(doc), {"Version", "Statement", "Id"}, f)
            for st in doc["Statement"]:
                self.assertLessEqual(set(st), {"Sid", "Effect", "Principal", "NotPrincipal", "Action",
                                               "NotAction", "Resource", "NotResource", "Condition"}, f)

    def test_execution_policy_has_no_user_delegation(self):
        doc = json.loads(cs.fill("iam/runtime-execution-policy.json", REGION="r", ACCOUNT="1"))
        actions = [a for st in doc["Statement"] for a in
                   (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])]
        self.assertNotIn("bedrock-agentcore:GetWorkloadAccessTokenForUserId", actions)
        self.assertFalse([a for a in actions if a.endswith(":*") or a == "*"], actions)


if __name__ == "__main__":
    unittest.main()
