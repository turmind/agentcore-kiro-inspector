#!/usr/bin/env python3
"""
Offline tests for build/notify.py and the delivery path in build/server.py.

No real WeCom / Feishu credentials and no Kiro binary needed:
  * a local HTTP server stands in for both qyapi.weixin.qq.com and open.feishu.cn
    (WECOM_WEBHOOK_KEY accepts a full URL, LARK_API_BASE is configurable);
  * a local websocket server stands in for wss://openws.work.weixin.qq.com
    (WECOM_WS_URL), speaking the 智能机器人长连接 subscribe / send_msg frames;
  * a fake kiro-cli-chat (shell script) writes the report file the prompt asks
    for and dumps its own environment, so we can check secrets were stripped.

    python3 tests/test_notify.py -v
"""

import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "build"))

import notify  # noqa: E402

CHANNEL_ENVS = (
    "WECOM_WEBHOOK_KEY", "WECOM_MSGTYPE", "WECOM_MAX_CHUNKS", "WECOM_CHANNEL",
    "WECOM_BOT_ID", "WECOM_BOT_SECRET", "WECOM_CHAT_ID", "WECOM_CHAT_TYPE", "WECOM_WS_URL",
    "LARK_APP_ID", "LARK_APP_KEY", "LARK_RECEIVE_ID", "LARK_RECEIVE_ID_TYPE",
    "LARK_API_BASE", "LARK_CHANNEL",
)


class Fake(BaseHTTPRequestHandler):
    calls = []          # (path, body)
    wecom_errcode = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.calls.append((self.path, body, dict(self.headers)))
        if self.path.startswith("/cgi-bin/webhook/send"):
            out = {"errcode": Fake.wecom_errcode, "errmsg": "ok" if not Fake.wecom_errcode else "bad"}
        elif self.path.endswith("/tenant_access_token/internal"):
            ok = body.get("app_secret") == "lark-secret"
            out = {"code": 0, "tenant_access_token": "t-123"} if ok else {"code": 10014, "msg": "app secret invalid"}
        elif self.path.startswith("/open-apis/im/v1/messages"):
            out = {"code": 0, "data": {"message_id": "om_fake"}}
        else:
            out = {"code": -1}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeBot:
    """Minimal openws.work.weixin.qq.com: acks by req_id, like the real one."""
    frames = []              # every frame the client sent
    subscribe_errcode = 0
    send_errcode = 0
    mode = "ok"              # ok | noisy (callbacks before acks) | kicked | silent

    @classmethod
    def reset(cls):
        cls.frames.clear()
        cls.subscribe_errcode = cls.send_errcode = 0
        cls.mode = "ok"

    @classmethod
    def handler(cls, ws):
        from websockets.exceptions import ConnectionClosed
        try:
            cls._serve(ws)
        except ConnectionClosed:  # client gave up (timeout / kicked tests) -> 1011
            pass

    @classmethod
    def _serve(cls, ws):
        for raw in ws:
            m = json.loads(raw)
            cls.frames.append(m)
            rid = m["headers"]["req_id"]
            if cls.mode == "silent":
                continue
            if cls.mode == "noisy":
                ws.send(json.dumps({"cmd": "aibot_msg_callback", "headers": {"req_id": "other"},
                                    "body": {"msgtype": "text"}}))
                ws.send("not json")
            if cls.mode == "kicked" and m["cmd"] == "aibot_send_msg":
                ws.send(json.dumps({"cmd": "aibot_event_callback", "headers": {"req_id": "e"},
                                    "body": {"event": {"eventtype": "disconnected_event"}}}))
                continue
            if m["cmd"] == "aibot_subscribe":
                ok = m["body"].get("secret") == "bot-secret" and not cls.subscribe_errcode
                code = 0 if ok else (cls.subscribe_errcode or 853000)
                # The real errmsg does not echo the secret; this one does, to test scrubbing.
                msg = "ok" if ok else f"invalid bot_id or secret {m['body'].get('secret')}"
                ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": code, "errmsg": msg}))
            elif m["cmd"] == "aibot_send_msg":
                code = cls.send_errcode
                ws.send(json.dumps({"headers": {"req_id": rid}, "errcode": code,
                                    "errmsg": "ok" if not code else "no conversation"}))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        from websockets.sync.server import serve
        cls.ws_srv = serve(FakeBot.handler, "127.0.0.1", 0)
        cls.ws_url = f"ws://127.0.0.1:{cls.ws_srv.socket.getsockname()[1]}"
        threading.Thread(target=cls.ws_srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.ws_srv.shutdown()

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in CHANNEL_ENVS}
        Fake.calls.clear()
        Fake.wecom_errcode = 0
        FakeBot.reset()

    def bot_on(self):
        os.environ.update(WECOM_BOT_ID="aib-bot-id", WECOM_BOT_SECRET="bot-secret",
                          WECOM_CHAT_ID="zhangsan", WECOM_WS_URL=self.ws_url)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def wecom_on(self):
        os.environ["WECOM_WEBHOOK_KEY"] = f"{self.base}/cgi-bin/webhook/send?key=wecom-secret"

    def lark_on(self):
        os.environ.update(LARK_APP_ID="cli_x", LARK_APP_KEY="lark-secret",
                          LARK_RECEIVE_ID="oc_x", LARK_API_BASE=self.base)


class Split(unittest.TestCase):
    def test_never_over_limit_and_lossless(self):
        text = "\n".join(f"- 第{i}行 资源 arn:aws:rds:ap-northeast-1:1:db/x{i} 维护待处理" for i in range(400))
        for limit in (100, 1000, 4080):
            parts = notify.split_utf8(text, limit)
            self.assertTrue(all(len(p.encode()) <= limit for p in parts))
            self.assertEqual("\n".join(parts).replace("\n", ""), text.replace("\n", ""))

    def test_long_line_cut_on_char_boundary(self):
        line = "中" * 3000  # 9000 bytes, no newline
        parts = notify.split_utf8(line, 4080)
        self.assertEqual("".join(parts), line)
        self.assertTrue(all(len(p.encode()) <= 4080 for p in parts))
        for p in parts:
            p.encode("utf-8").decode("utf-8")  # no broken code point

    def test_chunks_prefix_and_truncate(self):
        text = "\n".join("x" * 200 for _ in range(200))  # ~40 KB
        parts = notify.wecom_chunks(text, "markdown_v2", 3)
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].startswith("(1/3)\n"))
        self.assertIn("已截断", parts[-1])
        self.assertTrue(all(len(p.encode()) <= 4096 for p in parts))

    def test_single_chunk_has_no_prefix(self):
        self.assertEqual(notify.wecom_chunks("hello", "text", 5), ["hello"])

    def test_text_limit_is_2048(self):
        parts = notify.wecom_chunks("好" * 2000, "text", 10)  # 6000 bytes
        self.assertTrue(all(len(p.encode()) <= 2048 for p in parts))
        self.assertEqual(len(parts), 3)


class Channels(Base):
    def test_matrix(self):
        self.assertEqual(notify.enabled_channels(), [])
        self.wecom_on()
        self.assertEqual(notify.enabled_channels(), ["wecom"])
        self.lark_on()
        self.assertEqual(notify.enabled_channels(), ["wecom", "lark"])
        os.environ["WECOM_CHANNEL"] = "OFF"
        self.assertEqual(notify.enabled_channels(), ["lark"])
        os.environ["LARK_CHANNEL"] = "off"
        self.assertEqual(notify.enabled_channels(), [])

    def test_lark_needs_both_id_and_secret(self):
        os.environ["LARK_APP_ID"] = "cli_x"
        self.assertEqual(notify.enabled_channels(), [])

    def test_bare_key_builds_official_url(self):
        os.environ["WECOM_WEBHOOK_KEY"] = "abc"
        self.assertEqual(notify.wecom_url(), "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc")


class Send(Base):
    def test_both_channels(self):
        self.wecom_on()
        self.lark_on()
        res = notify.send_report("# 巡检\n- 无")
        self.assertEqual([r["channel"] for r in res], ["wecom", "lark"])
        self.assertTrue(all(r["ok"] for r in res), res)
        self.assertEqual(res[1]["message_id"], "om_fake")
        wecom = [c for c in Fake.calls if c[0].startswith("/cgi-bin")][0][1]
        self.assertEqual(wecom, {"msgtype": "markdown_v2", "markdown_v2": {"content": "# 巡检\n- 无"}})
        msg = [c for c in Fake.calls if "/im/v1/messages" in c[0]][0]
        self.assertIn("receive_id_type=chat_id", msg[0])
        self.assertEqual(msg[2]["Authorization"], "Bearer t-123")
        self.assertEqual(json.loads(msg[1]["content"])["text"], "# 巡检\n- 无")

    def test_multi_chunk_is_rate_spaced(self):
        self.wecom_on()
        t0 = time.time()
        res = notify.send_report("\n".join("y" * 300 for _ in range(30)))  # ~9 KB -> 3 msgs
        self.assertEqual(res[0]["messages"], 3)
        self.assertGreaterEqual(time.time() - t0, 2.0)

    def test_wecom_error_is_reported_and_key_scrubbed(self):
        self.wecom_on()
        Fake.wecom_errcode = 93000
        res = notify.send_report("x")
        self.assertFalse(res[0]["ok"])
        self.assertEqual(res[0]["errcode"], 93000)
        self.assertNotIn("wecom-secret", json.dumps(res))

    def test_unreachable_never_raises_and_scrubs(self):
        os.environ["WECOM_WEBHOOK_KEY"] = "http://127.0.0.1:9/cgi-bin/webhook/send?key=wecom-secret"
        res = notify.send_report("x")
        self.assertFalse(res[0]["ok"])
        self.assertNotIn("wecom-secret", json.dumps(res))

    def test_lark_bad_secret(self):
        self.lark_on()
        os.environ["LARK_APP_KEY"] = "wrong"
        res = notify.send_report("x")
        self.assertEqual((res[0]["ok"], res[0]["step"], res[0]["code"]), (False, "token", 10014))

    def test_empty_text_sends_nothing(self):
        self.wecom_on()
        self.assertEqual(notify.send_report("  \n"), [])
        self.assertEqual(Fake.calls, [])


class WecomBot(Base):
    def test_bot_enables_wecom_and_wins_over_webhook(self):
        os.environ.update(WECOM_BOT_ID="aib-bot-id")
        self.assertEqual(notify.enabled_channels(), [])       # secret missing
        self.bot_on()
        self.assertEqual((notify.enabled_channels(), notify.wecom_mode()), (["wecom"], "bot"))
        self.wecom_on()
        self.assertEqual(notify.wecom_mode(), "bot")
        res = notify.send_report("x")
        self.assertEqual(res[0]["mode"], "bot")
        self.assertEqual(Fake.calls, [])                       # webhook not used
        os.environ["WECOM_CHANNEL"] = "OFF"
        self.assertEqual(notify.enabled_channels(), [])

    def test_frames_match_the_protocol(self):
        self.bot_on()
        res = notify.send_report("# 巡检\n- 无")
        self.assertEqual(res, [{"channel": "wecom", "ok": True, "mode": "bot",
                                "messages": 1, "msgtype": "markdown"}])
        sub, send = FakeBot.frames
        self.assertEqual(sub["cmd"], "aibot_subscribe")
        self.assertEqual(sub["body"], {"bot_id": "aib-bot-id", "secret": "bot-secret"})
        self.assertRegex(sub["headers"]["req_id"], r"^aibot_subscribe_\d{13}_[0-9a-f]{8}$")
        self.assertEqual(send["cmd"], "aibot_send_msg")
        self.assertEqual(send["body"], {"chatid": "zhangsan", "chat_type": 1, "msgtype": "markdown",
                                        "markdown": {"content": "# 巡检\n- 无"}})
        self.assertNotEqual(sub["headers"]["req_id"], send["headers"]["req_id"])

    def test_group_chat_type(self):
        self.bot_on()
        os.environ.update(WECOM_CHAT_TYPE="2", WECOM_CHAT_ID="wrk-group")
        self.assertTrue(notify.send_report("x")[0]["ok"])
        self.assertEqual(FakeBot.frames[1]["body"]["chat_type"], 2)
        os.environ["WECOM_CHAT_TYPE"] = "9"
        res = notify.send_report("x")
        self.assertFalse(res[0]["ok"])
        self.assertIn("WECOM_CHAT_TYPE", res[0]["error"])

    def test_callbacks_and_garbage_before_ack_are_skipped(self):
        self.bot_on()
        FakeBot.mode = "noisy"
        self.assertTrue(notify.send_report("x")[0]["ok"])

    def test_subscribe_failure_scrubs_secret(self):
        self.bot_on()
        os.environ["WECOM_BOT_SECRET"] = "wrong-bot-secret"
        res = notify.send_report("x")
        self.assertEqual((res[0]["ok"], res[0]["step"], res[0]["errcode"]), (False, "subscribe", 853000))
        self.assertNotIn("wrong-bot-secret", json.dumps(res))
        self.assertEqual([f["cmd"] for f in FakeBot.frames], ["aibot_subscribe"])  # nothing sent

    def test_send_errcode_reported(self):
        self.bot_on()
        FakeBot.send_errcode = 846609
        res = notify.send_report("x")
        self.assertEqual((res[0]["ok"], res[0]["step"], res[0]["errcode"], res[0]["messages"]),
                         (False, "send", 846609, 0))

    def test_long_report_is_chunked_at_20480_bytes(self):
        self.bot_on()
        text = "\n".join("巡" * 100 for _ in range(100))          # ~30 KB
        res = notify.send_report(text)
        self.assertEqual(res[0]["messages"], 2)
        parts = [f["body"]["markdown"]["content"] for f in FakeBot.frames if f["cmd"] == "aibot_send_msg"]
        self.assertTrue(all(len(p.encode()) <= 20480 for p in parts))
        self.assertTrue(parts[0].startswith("(1/2)\n") and parts[1].startswith("(2/2)\n"))
        self.assertEqual(sum(1 for f in FakeBot.frames if f["cmd"] == "aibot_subscribe"), 1)

    def test_missing_chat_id(self):
        self.bot_on()
        del os.environ["WECOM_CHAT_ID"]
        res = notify.send_report("x")
        self.assertEqual((res[0]["ok"], res[0]["error"]), (False, "WECOM_CHAT_ID is not set"))
        self.assertEqual(FakeBot.frames, [])

    def test_kicked_by_another_connection(self):
        self.bot_on()
        FakeBot.mode = "kicked"
        res = notify.send_report("x")
        self.assertFalse(res[0]["ok"])
        self.assertIn("disconnected_event", res[0]["error"])

    def test_no_ack_times_out(self):
        self.bot_on()
        FakeBot.mode = "silent"
        saved, notify.WS_TIMEOUT = notify.WS_TIMEOUT, 0.5
        try:
            res = notify.send_report("x")
        finally:
            notify.WS_TIMEOUT = saved
        self.assertEqual((res[0]["ok"], res[0]["step"]), (False, "subscribe"))
        self.assertIn("TimeoutError", res[0]["error"])

    def test_unreachable_never_raises_and_scrubs(self):
        self.bot_on()
        os.environ["WECOM_WS_URL"] = "ws://127.0.0.1:9/bot-secret"
        res = notify.send_report("x")
        self.assertEqual((res[0]["ok"], res[0]["step"]), (False, "connect"))
        self.assertNotIn("bot-secret", json.dumps(res))


FAKE_KIRO = r"""#!/bin/sh
# Stand-in for kiro-cli-chat: find the report path in the prompt (last arg),
# write a report there, dump env for the secret check.
for last; do :; done
env > "$HOME/kiro-env.txt"
cp "$HOME/kiro-env.txt" "$KIRO_ENV_DUMP"
path=$(printf '%s' "$last" | grep -o '/[^`]*report-[0-9]*\.md' | head -1)
if [ -n "$path" ] && [ -z "$FAKE_NO_REPORT" ]; then
  printf '# 巡检结论\n- 无待处理事项\n' > "$path"
fi
sleep "${FAKE_SLEEP:-0}"
printf '> 报告已写入 %s\n' "$path"
exit "${FAKE_EXIT:-0}"
"""


class Bridge(Base):
    """server.py end to end with a fake kiro binary and fake channel endpoints."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="bridge-test-")
        kiro = os.path.join(cls.tmp, "kiro-cli-chat")
        with open(kiro, "w") as f:
            f.write(FAKE_KIRO)
        os.chmod(kiro, os.stat(kiro).st_mode | stat.S_IEXEC)
        os.environ.update(KIRO_BIN=kiro, KIRO_API_KEY="k", KIRO_ENV_DUMP=os.path.join(cls.tmp, "env.txt"))
        import server  # noqa: E402  (reads KIRO_BIN at import)
        cls.server = server
        cls.client = server.app.test_client()

    def env_dump(self):
        with open(os.environ["KIRO_ENV_DUMP"]) as f:
            return f.read()

    def test_sync_both_channels_and_secrets_stripped(self):
        self.wecom_on()
        self.lark_on()
        r = self.client.post("/invocations", data=json.dumps({"prompt": "查维护"}))  # no content-type
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["channels"], ["wecom", "lark"])
        self.assertTrue(all(d["ok"] for d in body["delivery"]), body["delivery"])
        sent = [c[1] for c in Fake.calls if c[0].startswith("/cgi-bin")][0]
        self.assertEqual(sent["markdown_v2"]["content"], "# 巡检结论\n- 无待处理事项")
        env = self.env_dump()
        for secret in ("wecom-secret", "lark-secret", "LARK_APP_ID=", "WECOM_WEBHOOK_KEY="):
            self.assertNotIn(secret, env)
        self.assertIn("LARK_RECEIVE_ID=", env)  # non-secret settings still pass through

    def test_sync_bot_mode_and_bot_secrets_stripped(self):
        self.bot_on()
        body = self.client.post("/invocations", json={"prompt": "查维护"}).get_json()
        self.assertEqual(body["channels"], ["wecom"])
        self.assertEqual(body["delivery"][0]["mode"], "bot")
        self.assertTrue(body["delivery"][0]["ok"], body["delivery"])
        sent = [f for f in FakeBot.frames if f["cmd"] == "aibot_send_msg"][0]
        self.assertEqual(sent["body"]["markdown"]["content"], "# 巡检结论\n- 无待处理事项")
        env = self.env_dump()
        for secret in ("bot-secret", "aib-bot-id", "WECOM_BOT_SECRET=", "WECOM_BOT_ID="):
            self.assertNotIn(secret, env)
        self.assertIn("WECOM_CHAT_ID=", env)

    def test_no_channel_no_delivery_instruction(self):
        r = self.client.post("/invocations", json={"prompt": "查维护"})
        body = r.get_json()
        self.assertEqual((body["channels"], body["delivery"]), ([], []))
        self.assertEqual(Fake.calls, [])

    def test_payload_narrows_channels(self):
        self.wecom_on()
        self.lark_on()
        body = self.client.post("/invocations", json={"prompt": "p", "channels": ["lark"]}).get_json()
        self.assertEqual(body["channels"], ["lark"])
        body = self.client.post("/invocations", json={"prompt": "p", "notify": False}).get_json()
        self.assertEqual(body["channels"], [])

    def test_fallback_to_final_answer_when_no_report(self):
        self.wecom_on()
        os.environ["FAKE_NO_REPORT"] = "1"
        try:
            self.client.post("/invocations", json={"prompt": "p"})
        finally:
            del os.environ["FAKE_NO_REPORT"]
        sent = [c[1] for c in Fake.calls if c[0].startswith("/cgi-bin")][0]
        self.assertTrue(sent["markdown_v2"]["content"].startswith("报告已写入"))

    def test_failure_still_notifies(self):
        self.wecom_on()
        os.environ.update(FAKE_NO_REPORT="1", FAKE_EXIT="3")
        try:
            body = self.client.post("/invocations", json={"prompt": "p"}).get_json()
        finally:
            del os.environ["FAKE_NO_REPORT"], os.environ["FAKE_EXIT"]
        self.assertEqual(body["status"], "error")
        sent = [c[1] for c in Fake.calls if c[0].startswith("/cgi-bin")][0]
        self.assertIn("巡检未完成（exit_code=3）", sent["markdown_v2"]["content"])

    def test_async_accepts_then_busy_then_delivers(self):
        self.wecom_on()
        os.environ["FAKE_SLEEP"] = "2"
        try:
            t0 = time.time()
            r = self.client.post("/invocations", json={"input": {"prompt": "p", "async": True}})
            self.assertLess(time.time() - t0, 1.0)
            body = r.get_json()
            self.assertEqual(body["status"], "accepted")
            self.assertTrue(re.match(r"job-\d+", body["job_id"]))
            p1 = self.client.get("/ping").get_json()
            self.assertEqual(p1["status"], "HealthyBusy")
            for _ in range(50):
                time.sleep(0.2)
                if self.client.get("/ping").get_json()["status"] == "Healthy":
                    break
            p2 = self.client.get("/ping").get_json()
            self.assertEqual(p2["status"], "Healthy")
            self.assertGreaterEqual(p2["time_of_last_update"], p1["time_of_last_update"])
            # time_of_last_update only moves on a state change
            time.sleep(1.1)
            self.assertEqual(self.client.get("/ping").get_json()["time_of_last_update"],
                             p2["time_of_last_update"])
        finally:
            del os.environ["FAKE_SLEEP"]
        self.assertEqual(len([c for c in Fake.calls if c[0].startswith("/cgi-bin")]), 1)

    def test_ping_has_no_startup_timestamp(self):
        # Nothing captured at import time (Runtime V2 snapshots module state).
        saved, self.server._STATE_SINCE = self.server._STATE_SINCE, None
        try:
            body = self.client.get("/ping").get_json()
            self.assertEqual(body["status"], "Healthy")
            self.assertNotIn("time_of_last_update", body)
        finally:
            self.server._STATE_SINCE = saved

    def test_empty_prompt_400(self):
        self.assertEqual(self.client.post("/invocations", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
