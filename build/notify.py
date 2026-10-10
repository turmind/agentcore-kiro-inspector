#!/usr/bin/env python3
"""
Report delivery for the inspector: WeCom + Feishu.

WeCom has two ways in; the 智能机器人 long connection wins when both are set.

  WeCom 智能机器人 (long connection, wss://openws.work.weixin.qq.com):
  WECOM_BOT_ID           bot id            } both needed
  WECOM_BOT_SECRET       bot secret        }
  WECOM_CHAT_ID          target: the user's userid (single chat) or a group chatid
  WECOM_CHAT_TYPE        1 = single chat (default) | 2 = group | 0 = let WeCom decide
  WECOM_WS_URL           override the endpoint (tests only)
    The user must have messaged the bot at least once before it can push.
    One connection per report: connect, subscribe, send, close. Only one
    connection per bot is kept by WeCom, so do not share the bot with a
    long-running client (the newer connection kicks the older one out).

  WeCom 消息推送 (group webhook):
  WECOM_WEBHOOK_KEY      the key=... part of the webhook URL, or the full URL
  WECOM_MSGTYPE          markdown_v2 (default) | markdown | text

  WECOM_MAX_CHUNKS       max messages per report, default 5 (both modes)
  WECOM_CHANNEL          OFF -> WeCom disabled even if configured

  LARK_APP_ID            Feishu app id
  LARK_APP_KEY           Feishu app secret
  LARK_RECEIVE_ID        target chat/user id
  LARK_RECEIVE_ID_TYPE   chat_id (default) | open_id | user_id | union_id | email
  LARK_API_BASE          https://open.feishu.cn (default); Lark intl: https://open.larksuite.com
  LARK_CHANNEL           OFF -> disabled even if app id/key are set

WeCom bot id + secret (or webhook key) set -> WeCom. Feishu app id + secret
set -> Feishu. Both -> both. Neither -> nothing is sent.

As a module (what server.py does):

    import notify
    notify.enabled_channels()        # -> ["wecom", "lark"]
    results = notify.send_report(text)
    # -> [{"channel": "wecom", "ok": True, "messages": 1}, {"channel": "lark", ...}]

From a shell (manual test):

    python3 notify.py --channels
    python3 notify.py --file report.md [--dry-run]
    echo "hello" | python3 notify.py

Standard library plus `websockets` (only imported when the WeCom bot is used).
send_report() never raises: every failure comes back as ok=False with the
error text, and the bot id / bot secret / webhook key / app secret never
appear in results.
"""

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

WECOM_SEND = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
WECOM_WS = "wss://openws.work.weixin.qq.com"

# Long-connection push: msgtype markdown, content at most 20480 utf-8 bytes;
# 30 messages/minute and 1000/hour per conversation
# (https://developer.work.weixin.qq.com/document/path/101463).
WECOM_BOT_LIMIT = 20480
WS_TIMEOUT = 10

# Byte limits of the "content" field, utf-8, from the WeCom 消息推送 doc
# (https://developer.work.weixin.qq.com/document/path/91770).
WECOM_LIMITS = {"text": 2048, "markdown": 4096, "markdown_v2": 4096}

HTTP_TIMEOUT = 10


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _off(name: str) -> bool:
    return _env(name).upper() == "OFF"


# ------------------------------------------------------------------ channels


def wecom_bot_configured() -> bool:
    return bool(_env("WECOM_BOT_ID")) and bool(_env("WECOM_BOT_SECRET"))


def wecom_mode() -> str:
    """"bot" (long connection), "webhook", or "" when WeCom is not configured."""
    if wecom_bot_configured():
        return "bot"
    if _env("WECOM_WEBHOOK_KEY"):
        return "webhook"
    return ""


def wecom_enabled() -> bool:
    return bool(wecom_mode()) and not _off("WECOM_CHANNEL")


def lark_enabled() -> bool:
    return bool(_env("LARK_APP_ID")) and bool(_env("LARK_APP_KEY")) and not _off("LARK_CHANNEL")


def enabled_channels() -> list:
    out = []
    if wecom_enabled():
        out.append("wecom")
    if lark_enabled():
        out.append("lark")
    return out


def _secrets() -> list:
    names = ("WECOM_BOT_SECRET", "WECOM_BOT_ID", "WECOM_WEBHOOK_KEY", "LARK_APP_KEY")
    return [v for v in (_env(n) for n in names) if v]


def _scrub(msg: str) -> str:
    for s in _secrets():
        msg = msg.replace(s, "***")
    return msg


def _post_json(url: str, body: dict, headers: dict = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        # Both APIs put the real reason in a JSON body even on 4xx/5xx.
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeError(f"HTTP {e.code}: {raw[:300]}")


# --------------------------------------------------------------- splitting


def split_utf8(text: str, limit: int) -> list:
    """
    Split text into pieces of at most `limit` utf-8 bytes, breaking at line
    boundaries where possible and never inside a multi-byte character.
    """
    chunks, cur, cur_len = [], [], 0

    def flush():
        nonlocal cur, cur_len
        if cur:
            chunks.append("".join(cur).rstrip("\n"))
        cur, cur_len = [], 0

    for line in text.splitlines(keepends=True):
        n = len(line.encode("utf-8"))
        if n > limit:
            # One line longer than a whole message: cut it by characters.
            flush()
            piece, plen = [], 0
            for ch in line:
                cb = len(ch.encode("utf-8"))
                if plen + cb > limit:
                    chunks.append("".join(piece))
                    piece, plen = [], 0
                piece.append(ch)
                plen += cb
            if piece:
                cur, cur_len = piece, plen
            continue
        if cur_len + n > limit:
            flush()
        cur.append(line)
        cur_len += n
    flush()
    return [c for c in chunks if c.strip()]


def wecom_chunks(text: str, msgtype: str, max_chunks: int, limit: int = 0) -> list:
    limit = limit or WECOM_LIMITS[msgtype]
    # Reserve room for the "(i/n)\n" prefix added when there is more than one.
    parts = split_utf8(text, limit - 16)
    if len(parts) > max_chunks:
        parts = parts[:max_chunks]
        note = "\n（后续内容超出条数上限，已截断）"
        body = parts[-1]
        while len((body + note).encode("utf-8")) > limit - 16:
            body = body[:-1]
        parts[-1] = body + note
    if len(parts) > 1:
        parts = [f"({i}/{len(parts)})\n{p}" for i, p in enumerate(parts, 1)]
    return parts


# ------------------------------------------------------------------ senders


def wecom_url() -> str:
    v = _env("WECOM_WEBHOOK_KEY")
    return v if v.startswith("http") else WECOM_SEND + v


def send_wecom_webhook(text: str) -> dict:
    msgtype = _env("WECOM_MSGTYPE") or "markdown_v2"
    if msgtype not in WECOM_LIMITS:
        return {"channel": "wecom", "ok": False, "error": f"unsupported WECOM_MSGTYPE={msgtype}"}
    parts = wecom_chunks(text, msgtype, _max_chunks())
    sent = 0
    for i, part in enumerate(parts):
        if i:
            time.sleep(1)  # keep well under 20 msg/min
        try:
            r = _post_json(wecom_url(), {"msgtype": msgtype, msgtype: {"content": part}})
        except Exception as e:  # network, DNS, TLS, non-JSON body
            return {"channel": "wecom", "ok": False, "messages": sent,
                    "error": _scrub(f"{type(e).__name__}: {e}")}
        if r.get("errcode") != 0:
            return {"channel": "wecom", "ok": False, "messages": sent,
                    "errcode": r.get("errcode"), "error": _scrub(str(r.get("errmsg", "")))}
        sent += 1
    return {"channel": "wecom", "ok": True, "mode": "webhook", "messages": sent, "msgtype": msgtype}


def _max_chunks() -> int:
    try:
        return max(1, int(_env("WECOM_MAX_CHUNKS") or "5"))
    except ValueError:
        return 5


def _req_id(cmd: str) -> str:
    # Same shape as the official SDK: {cmd}_{ms timestamp}_{random}
    return f"{cmd}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def _ws_call(ws, cmd: str, body: dict, timeout: float = None) -> dict:
    """
    Send one request frame and wait for its ack. Acks carry no cmd, only the
    same headers.req_id, so anything else that arrives meanwhile (message or
    event callbacks) is skipped; a disconnected_event means another
    connection took the bot over.
    """
    timeout = timeout or WS_TIMEOUT
    rid = _req_id(cmd)
    ws.send(json.dumps({"cmd": cmd, "headers": {"req_id": rid}, "body": body}, ensure_ascii=False))
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"no ack for {cmd} within {timeout:.0f}s")
        raw = ws.recv(timeout=left)
        try:
            m = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(m, dict):
            continue
        if (m.get("headers") or {}).get("req_id") == rid:
            return m
        if m.get("cmd") == "aibot_event_callback" and "disconnected_event" in json.dumps(m):
            raise ConnectionError("disconnected_event: another connection took over this bot")


def send_wecom_bot(text: str) -> dict:
    chat_id = _env("WECOM_CHAT_ID")
    if not chat_id:
        return {"channel": "wecom", "ok": False, "mode": "bot", "error": "WECOM_CHAT_ID is not set"}
    try:
        chat_type = int(_env("WECOM_CHAT_TYPE") or "1")
    except ValueError:
        chat_type = -1
    if chat_type not in (0, 1, 2):
        return {"channel": "wecom", "ok": False, "mode": "bot",
                "error": "WECOM_CHAT_TYPE must be 0, 1 or 2"}

    parts = wecom_chunks(text, "markdown", _max_chunks(), WECOM_BOT_LIMIT)
    sent, step = 0, "connect"
    try:
        from websockets.sync.client import connect

        with connect(_env("WECOM_WS_URL") or WECOM_WS, open_timeout=WS_TIMEOUT,
                     close_timeout=5, ping_interval=None) as ws:
            step = "subscribe"
            r = _ws_call(ws, "aibot_subscribe",
                         {"bot_id": _env("WECOM_BOT_ID"), "secret": _env("WECOM_BOT_SECRET")})
            if r.get("errcode") != 0:
                return {"channel": "wecom", "ok": False, "mode": "bot", "step": step, "messages": 0,
                        "errcode": r.get("errcode"), "error": _scrub(str(r.get("errmsg", "")))}
            step = "send"
            for i, part in enumerate(parts):
                if i:
                    time.sleep(0.5)  # 30 msg/min per conversation
                r = _ws_call(ws, "aibot_send_msg",
                             {"chatid": chat_id, "chat_type": chat_type,
                              "msgtype": "markdown", "markdown": {"content": part}})
                if r.get("errcode") != 0:
                    return {"channel": "wecom", "ok": False, "mode": "bot", "step": step,
                            "messages": sent, "errcode": r.get("errcode"),
                            "error": _scrub(str(r.get("errmsg", "")))}
                sent += 1
    except Exception as e:  # import, DNS, TLS, handshake, timeout, closed
        return {"channel": "wecom", "ok": False, "mode": "bot", "step": step, "messages": sent,
                "error": _scrub(f"{type(e).__name__}: {e}")}
    return {"channel": "wecom", "ok": True, "mode": "bot", "messages": sent, "msgtype": "markdown"}


def send_wecom(text: str) -> dict:
    return send_wecom_bot(text) if wecom_mode() == "bot" else send_wecom_webhook(text)


def send_lark(text: str) -> dict:
    base = (_env("LARK_API_BASE") or "https://open.feishu.cn").rstrip("/")
    receive_id = _env("LARK_RECEIVE_ID")
    id_type = _env("LARK_RECEIVE_ID_TYPE") or "chat_id"
    if not receive_id:
        return {"channel": "lark", "ok": False, "error": "LARK_RECEIVE_ID is not set"}
    try:
        t = _post_json(f"{base}/open-apis/auth/v3/tenant_access_token/internal",
                       {"app_id": _env("LARK_APP_ID"), "app_secret": _env("LARK_APP_KEY")})
        if t.get("code") != 0 or not t.get("tenant_access_token"):
            return {"channel": "lark", "ok": False, "step": "token",
                    "code": t.get("code"), "error": _scrub(str(t.get("msg", "")))}
        r = _post_json(
            f"{base}/open-apis/im/v1/messages?receive_id_type={id_type}",
            {"receive_id": receive_id, "msg_type": "text",
             "content": json.dumps({"text": text}, ensure_ascii=False)},
            {"Authorization": "Bearer " + t["tenant_access_token"]},
        )
    except Exception as e:
        return {"channel": "lark", "ok": False, "error": _scrub(f"{type(e).__name__}: {e}")}
    if r.get("code") != 0:
        return {"channel": "lark", "ok": False, "step": "send",
                "code": r.get("code"), "error": _scrub(str(r.get("msg", "")))}
    return {"channel": "lark", "ok": True, "message_id": (r.get("data") or {}).get("message_id", "")}


SENDERS = {"wecom": send_wecom, "lark": send_lark}


def send_report(text: str, channels: list = None) -> list:
    """Send `text` to every enabled channel (or the given subset). Never raises."""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    for ch in channels if channels is not None else enabled_channels():
        try:
            out.append(SENDERS[ch](text))
        except Exception as e:
            out.append({"channel": ch, "ok": False, "error": _scrub(f"{type(e).__name__}: {e}")})
    return out


# ---------------------------------------------------------------------- CLI


def main(argv: list) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Send a report to WeCom / Feishu (env-configured).")
    p.add_argument("--file", help="report file; default: read stdin")
    p.add_argument("--channels", action="store_true", help="print enabled channels and exit")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    a = p.parse_args(argv)

    if a.channels:
        print(json.dumps(enabled_channels()))
        return 0

    text = open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read()
    chans = enabled_channels()
    if not chans:
        print(json.dumps({"error": "no channel configured"}, ensure_ascii=False))
        return 2

    if a.dry_run:
        plan = {}
        if "wecom" in chans:
            if wecom_mode() == "bot":
                mt, parts = "markdown", wecom_chunks(text.strip(), "markdown", _max_chunks(),
                                                     WECOM_BOT_LIMIT)
            else:
                mt = _env("WECOM_MSGTYPE") or "markdown_v2"
                parts = wecom_chunks(text.strip(), mt, _max_chunks())
            plan["wecom"] = {"mode": wecom_mode(), "msgtype": mt, "messages": len(parts),
                             "bytes": [len(x.encode("utf-8")) for x in parts]}
        if "lark" in chans:
            plan["lark"] = {"messages": 1, "bytes": len(text.strip().encode("utf-8"))}
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    results = send_report(text, chans)
    print(json.dumps(results, ensure_ascii=False))
    return 0 if results and all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
