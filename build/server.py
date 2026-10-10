#!/usr/bin/env python3
"""
AgentCore Runtime <-> Kiro CLI bridge.

AgentCore contract:
  POST /invocations   -> run the agent, return JSON
  GET  /ping          -> health check, 200 + {"status":"Healthy"}
  listens on 0.0.0.0:8080

Everything about *what* to check comes from the invocation payload (prompt).
Everything about *how* to run comes from env vars:

  KIRO_API_KEY            (required) Kiro API key, headless auth
  KIRO_MODEL              model id, default gpt-5.6-luna
  KIRO_EFFORT             reasoning effort, default max
  KIRO_TRUST_TOOLS        comma list for --trust-tools; if unset -> --trust-all-tools
  KIRO_TIMEOUT_SECONDS    per-run wall clock, default 780 (< AgentCore 15min sync cap)

  KIRO_STATE_MODE         ephemeral (default) | session
                          ephemeral: throwaway HOME+cwd per call, zero memory.
                          session:   HOME+cwd keyed by AgentCore session id, and
                                     each call resumes the previous conversation.
  KIRO_STATE_ROOT         base dir for session state, default /tmp/kiro-sessions.
                          Point it at an EFS mountPath to survive microVM churn.
  KIRO_RESUME_STRATEGY    probe (default) | latest
                          probe:  validate the id via --list-sessions, then
                                  --resume-id <id>. Exact, one extra exec.
                          latest: bare --resume, no lookup. REQUIRED when
                                  STATE_ROOT is on EFS -- --list-sessions hangs
                                  there (60s of dead wait per call, twice per
                                  request) while kiro-cli's own I/O is fine.
  KIRO_AGENT              --agent <name>, selects a $HOME/.kiro/agents/<name>.json
                          profile (system prompt / tool allowlist / resources)

  Report delivery (see notify.py for the full list). A channel is on as soon as
  its credentials are present; both present -> both get the report:
  WECOM_BOT_ID            WeCom 智能机器人 bot id   } both needed; long connection,
  WECOM_BOT_SECRET        WeCom 智能机器人 secret   } wins over the webhook
  WECOM_CHAT_ID           userid (single chat) or group chatid to push to
  WECOM_CHAT_TYPE         1 single (default) | 2 group | 0 let WeCom decide
  WECOM_WEBHOOK_KEY       WeCom 消息推送 webhook key (or the full webhook URL)
  WECOM_MSGTYPE           markdown_v2 (default) | markdown | text  (webhook only)
  LARK_APP_ID             Feishu app id      } both needed for Feishu
  LARK_APP_KEY            Feishu app secret  }
  LARK_RECEIVE_ID         target chat/user id
  LARK_RECEIVE_ID_TYPE    chat_id | open_id | user_id | union_id | email  (default chat_id)
  WECOM_CHANNEL / LARK_CHANNEL = OFF  force a channel off without removing its key

  Sending is done by this bridge (notify.py), not by Kiro: Kiro writes the
  report to a file, the bridge pushes it. Channel secrets are stripped from
  Kiro's environment and never appear in the prompt.

Payload options (all optional, next to "prompt"):
  "async": true           return {"status":"accepted"} at once, run + deliver in
                          a background thread, /ping reports HealthyBusy until
                          done. Used by the EventBridge Scheduler job so the
                          scheduler never waits on a multi-minute sync call.
  "notify": false         don't send anything for this call
  "channels": ["wecom"]   only these of the enabled channels

Context persistence, as verified on real AgentCore (see README):
  * Calls carrying the same runtimeSessionId land on the SAME microVM, and its
    filesystem survives between calls. A different session id gets a fresh one.
  * Kiro CLI keeps conversations in $HOME/.local/share/kiro-cli/data.sqlite3,
    keyed by cwd + conversation id. So a stable HOME per session is what makes
    `--resume-id` work; the throwaway HOME of the ephemeral mode is exactly what
    wipes the memory.
  * `--resume-id` pointing at a conversation that does not exist in this HOME
    does NOT fail loudly -- it silently starts a new conversation and can drop
    into the v3 engine. So the id is validated against --list-sessions first.
  * Where the state dir lives decides how far the memory reaches:
      /tmp (container-local)  -> only while that microVM is alive
                                 (idleRuntimeSessionTimeout / maxLifetime).
      EFS access point        -> outlives the microVM. Needs networkMode=VPC
                                 (the API rejects PUBLIC outright) plus NAT for
                                 egress, and KIRO_RESUME_STRATEGY=latest.
      managed sessionStorage  -> do NOT put the sqlite there: every call dies
                                 with "Failed to open database: database is
                                 locked" in ~10s. EFS does not have this
                                 problem; only --list-sessions misbehaves.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from flask import Flask, jsonify, request

import notify

app = Flask(__name__)

KIRO_BIN = os.environ.get("KIRO_BIN", "/opt/kiro/bin/kiro-cli-chat")
DEFAULT_MODEL = os.environ.get("KIRO_MODEL", "gpt-5.6-luna")
DEFAULT_EFFORT = os.environ.get("KIRO_EFFORT", "max")
DEFAULT_TIMEOUT = int(os.environ.get("KIRO_TIMEOUT_SECONDS", "780"))
STATE_MODE = os.environ.get("KIRO_STATE_MODE", "ephemeral").strip().lower()
STATE_ROOT = os.environ.get("KIRO_STATE_ROOT", "/tmp/kiro-sessions")
# probe  = verify the id with --list-sessions, then --resume-id <id>
# latest = bare --resume, no lookup. Mandatory when STATE_ROOT is on EFS:
#          --list-sessions hangs on the sqlite read there (60s per call).
RESUME_STRATEGY = os.environ.get("KIRO_RESUME_STRATEGY", "probe").strip().lower()
KIRO_AGENT = os.environ.get("KIRO_AGENT", "").strip()

# ANSI escape sequences + the cursor/erase control noise the CLI emits.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


CHANNEL_NAMES = {"wecom": "企业微信", "lark": "飞书"}

# Channel credentials are for the bridge only. They are removed from Kiro's
# environment so the model can neither echo them nor send on its own.
NOTIFY_SECRET_ENVS = ("WECOM_BOT_ID", "WECOM_BOT_SECRET", "WECOM_WEBHOOK_KEY",
                      "LARK_APP_ID", "LARK_APP_KEY")


def lark_enabled() -> bool:
    # Kept for the "lark_channel" response field; the rule lives in notify.py.
    return notify.lark_enabled()


def delivery_instruction(report_path: str, channels: list) -> str:
    """
    Appended when at least one channel will receive this call's report.
    Kiro only writes the file; the bridge pushes it after Kiro exits.
    """
    names = "、".join(CHANNEL_NAMES[c] for c in channels)
    return f"""

---
## 报告交付

巡检结论整理完之后，把**要推送给人看的最终报告**写入文件 `{report_path}`（UTF-8，覆盖写）。
你结束之后，运行环境会把这个文件原样推送到：{names}。

- 不需要、也不要自己去发消息，运行环境里没有渠道凭据。
- 文件里只放报告本身：先结论后明细；不要放查询过程、命令或工具输出。
- 格式：纯文本，可以用 `#` 标题和 `-` 开头的列表；不要用表格。
- 最终回答里写一行「报告已写入 {report_path}」。
"""


BASE_SYSTEM = """你是一个 AWS 运维巡检 agent，运行在无人值守环境中。

工作方式：
- 你有 `use_aws` 工具可以直接调用 AWS API，用它取真实数据。只读操作直接做。
- 绝不执行任何写操作/变更操作（create/delete/modify/reboot/apply 等一律不做），只查、只读、只报告。
- 所有结论必须来自你实际调用 API 拿到的数据。查不到就写「未查到」，不要推测、不要编造资源名或事件 ID。
- 报告用中文，简洁，先给结论再给明细。没有待处理事项时明确说「无」，不要为了填充篇幅而罗列正常项。
"""


def build_prompt(user_prompt: str, report_path: str = "", channels: list = None) -> str:
    parts = [BASE_SYSTEM, "\n---\n## 本次巡检任务\n\n", user_prompt]
    if report_path and channels:
        parts.append(delivery_instruction(report_path, channels))
    return "".join(parts)


# ---------------------------------------------------------------- state layout


def session_dir(session_id: str) -> str:
    """
    Stable per-session HOME+cwd. Same AgentCore session id -> same microVM
    (verified) -> this directory is still there on the next call.
    """
    safe = _SAFE.sub("_", session_id)[:120] or "default"
    path = os.path.join(STATE_ROOT, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _kiro_env(home: str) -> dict:
    env = os.environ.copy()
    for k in NOTIFY_SECRET_ENVS:
        env.pop(k, None)
    env["HOME"] = home
    return env


def list_conversations(home: str, cwd: str) -> list:
    """
    `chat --list-sessions -f json` -> [{"cwd":..., "sessions":[{sessionId,...}]}]
    Returns the session dicts for this cwd, newest first.
    """
    try:
        proc = subprocess.run(
            [KIRO_BIN, "chat", "--list-sessions", "-f", "json"],
            cwd=cwd,
            env=_kiro_env(home),
            capture_output=True,
            text=True,
            timeout=60,
        )
        data = json.loads(strip_ansi(proc.stdout).strip() or "[]")
    except Exception:
        return []

    out = []
    for envelope in data if isinstance(data, list) else []:
        out.extend(envelope.get("sessions") or [])
    out.sort(key=lambda s: s.get("updatedAt") or "", reverse=True)
    return out


def resolve_resume_id(home: str, cwd: str, want: str) -> str:
    """
    Only return an id that actually exists in this HOME. Passing a stale id to
    --resume-id silently starts a brand new conversation (and can fall back to
    the v3 engine), which would look like "context was lost" with no error.
    """
    existing = {s.get("sessionId") for s in list_conversations(home, cwd)}
    if want and want in existing:
        return want
    return ""


def resume_flags(home: str, cwd: str, want: str) -> list:
    """
    How to ask kiro-cli to continue the previous conversation.

    Two strategies, because `--list-sessions` is not always usable:

      probe (default on a local disk)
        Verify the id exists first, then pass `--resume-id <id>`. Costs one
        extra kiro-cli invocation but is exact, and it is the only way to be
        safe: a non-existent id is accepted silently, starts a brand new
        conversation and drops to the v3 engine, with exit code 0.

      latest (required on NFS-backed state, e.g. an EFS access point)
        Pass bare `--resume`, which continues the newest conversation for this
        cwd without any lookup. On EFS, `--list-sessions` hangs forever on the
        sqlite read (measured: every call cost exactly 60s of bridge timeout,
        twice per request), while kiro-cli's own read/write path is fine. Since
        HOME+cwd are keyed by the AgentCore session id, "newest for this cwd"
        is already the right conversation.
    """
    if RESUME_STRATEGY == "latest":
        # Nothing to verify: no id is passed, so there is no stale-id hazard.
        # Only ask to resume if this session has run at least once.
        return ["--resume"] if want else []

    rid = resolve_resume_id(home, cwd, want)
    return ["--resume-id", rid] if rid else []


def conv_marker(home: str) -> str:
    return os.path.join(home, ".bridge-conversation-id")


def read_marker(home: str) -> str:
    try:
        with open(conv_marker(home)) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_marker(home: str, conv_id: str) -> None:
    try:
        with open(conv_marker(home), "w") as f:
            f.write(conv_id)
    except OSError:
        pass


# ------------------------------------------------------------------- execution


def run_kiro(
    prompt: str,
    model: str,
    effort: str,
    timeout: int,
    home: str,
    cwd: str,
    resume: list,
) -> dict:
    """Run kiro-cli headless. Caller decides whether home/cwd are throwaway."""
    cmd = [KIRO_BIN, "chat", "--no-interactive", "--model", model, "--effort", effort]

    if KIRO_AGENT:
        cmd += ["--agent", KIRO_AGENT]
    cmd += resume

    trust = os.environ.get("KIRO_TRUST_TOOLS", "").strip()
    if trust:
        cmd.append(f"--trust-tools={trust}")
    else:
        cmd.append("--trust-all-tools")

    cmd.append(prompt)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=_kiro_env(home),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out, err, code, timed_out = proc.stdout, proc.stderr, proc.returncode, False
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = (e.stderr or "") + f"\n[bridge] timed out after {timeout}s"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        code, timed_out = 124, True

    return {
        "exit_code": code,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 2),
        "output": strip_ansi(out).strip(),
        "stderr": strip_ansi(err).strip()[-4000:],
    }


# Only set when the health state actually changes. Refreshing it on every
# /ping keeps the session looking perpetually fresh so the idle timeout never
# fires and sessions live until maxLifetime, which burns through the session
# quota.
# Starts as None (field omitted from /ping, which the docs allow) instead of the
# process start time: on Runtime V2 module-level values are frozen into the
# snapshot, so a start-time stamp would report the snapshot time on every
# restored instance. The first real change happens inside a request handler,
# i.e. after the restore.
_STATE_SINCE = None
_BUSY = 0
_BUSY_LOCK = threading.Lock()


def _busy(delta: int) -> None:
    global _STATE_SINCE, _BUSY
    with _BUSY_LOCK:
        before = _BUSY > 0
        _BUSY += delta
        if (_BUSY > 0) != before:
            _STATE_SINCE = int(time.time())


@app.get("/ping")
def ping():
    body = {"status": "HealthyBusy" if _BUSY > 0 else "Healthy"}
    if _STATE_SINCE is not None:
        body["time_of_last_update"] = _STATE_SINCE
    return jsonify(body), 200


def extract_prompt(payload: dict) -> str:
    """
    AgentCore sends {"input": {"prompt": "..."}}. Accept that, plus the flatter
    shapes people try by hand.
    """
    inp = payload.get("input")
    if isinstance(inp, dict):
        for k in ("prompt", "text", "inputText", "query"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    elif isinstance(inp, str) and inp.strip():
        return inp.strip()

    for k in ("prompt", "inputText", "text", "query"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def payload_flag(payload: dict, key: str):
    inp = payload.get("input")
    if isinstance(inp, dict) and key in inp:
        return inp[key]
    return payload.get(key)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def pick_channels(payload: dict) -> list:
    """Enabled channels, narrowed by {"notify": false} / {"channels": [...]}."""
    flag = payload_flag(payload, "notify")
    if flag is not None and not _truthy(flag):
        return []
    enabled = notify.enabled_channels()
    wanted = payload_flag(payload, "channels")
    if isinstance(wanted, str):
        wanted = [w.strip() for w in wanted.split(",")]
    if isinstance(wanted, list):
        return [c for c in enabled if c in wanted]
    return enabled


def final_answer(output: str) -> str:
    """
    Last assistant message in kiro-cli's stdout. v1 prints each assistant turn
    as a "> " block, interleaved with tool-call noise; the last block is the
    answer. Only used when the report file was not written.
    """
    text = output.strip()
    if "\n> " in text:
        text = text.rsplit("\n> ", 1)[1]
    elif text.startswith("> "):
        text = text[2:]
    return text.strip()


def report_text(report_path: str, result: dict) -> str:
    """What gets pushed: the report file, else the final answer, else a failure notice."""
    try:
        with open(report_path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass

    if result["exit_code"] == 0:
        answer = final_answer(result["output"])
        if answer:
            return answer

    tail = final_answer(result["output"])[-1500:] or result["stderr"][-1500:]
    reason = "超时" if result["timed_out"] else f"exit_code={result['exit_code']}"
    return f"巡检未完成（{reason}），没有生成报告。\n\n{tail}".strip()


def execute(payload: dict, prompt: str, agentcore_session: str, channels: list) -> dict:
    """One full run: state dir -> kiro -> resume bookkeeping -> delivery -> cleanup."""
    model = payload_flag(payload, "model") or DEFAULT_MODEL
    effort = payload_flag(payload, "effort") or DEFAULT_EFFORT
    timeout = int(payload_flag(payload, "timeout_seconds") or DEFAULT_TIMEOUT)

    # Per-call override: {"state_mode": "session"} / "ephemeral", and
    # {"new_conversation": true} to start a fresh thread inside a live session.
    mode = (payload_flag(payload, "state_mode") or STATE_MODE).strip().lower()
    fresh = _truthy(payload_flag(payload, "new_conversation"))

    # {"thread": "<name>"} names the state directory explicitly instead of
    # deriving it from the AgentCore session id. On container-local storage this
    # buys nothing (a new session id means a new microVM with an empty /tmp),
    # but on EFS the directory outlives the microVM, so a caller that passes the
    # same thread name keeps one conversation going across sessions -- and
    # across days. Without it, "session" mode can never reach further than the
    # microVM, because the session id IS the directory name.
    thread = (payload_flag(payload, "thread") or "").strip()

    throwaway = False
    if mode == "session" and (thread or agentcore_session):
        home = cwd = session_dir(thread or agentcore_session)
        if fresh:
            write_marker(home, "")
        marker = read_marker(home)
        resume = resume_flags(home, cwd, marker)
    else:
        mode = "ephemeral"
        home = cwd = tempfile.mkdtemp(prefix="kiro-run-", dir="/tmp")
        marker = ""
        resume = []
        throwaway = True

    # Fresh name per call so a session dir never hands over last call's report.
    report_path = os.path.join(home, f"report-{int(time.time() * 1000)}.md")
    delivery = []

    _busy(+1)
    try:
        result = run_kiro(
            build_prompt(prompt, report_path, channels),
            model, effort, timeout, home, cwd, resume,
        )
        conv_id = marker
        if mode == "session":
            if RESUME_STRATEGY == "latest":
                # Can't look the id up here either (same sqlite read that hangs
                # on EFS). The marker only has to be non-empty so the next call
                # knows this session already has a conversation to resume.
                conv_id = marker or "latest"
                write_marker(home, conv_id)
            else:
                # Record the real conversation id so the next call resumes it.
                sessions = list_conversations(home, cwd)
                if sessions:
                    conv_id = sessions[0].get("sessionId") or conv_id
                    write_marker(home, conv_id)

        if channels:
            delivery = notify.send_report(report_text(report_path, result), channels)
    finally:
        try:
            os.remove(report_path)
        except OSError:
            pass
        if throwaway:
            shutil.rmtree(home, ignore_errors=True)
        _busy(-1)

    ok = result["exit_code"] == 0

    body = {
        # AgentCore HTTP contract fields.
        "response": result["output"],
        "status": "success" if ok else "error",
        # Extras for the caller.
        "model": model,
        "effort": effort,
        "channels": channels,
        "delivery": delivery,
        "lark_channel": "ON" if "lark" in channels else "OFF",
        "duration_seconds": result["duration_seconds"],
        "exit_code": result["exit_code"],
        "session_id": agentcore_session,
        "thread": thread,
        "state_mode": mode,
        "resumed": bool(resume),
        "resume_strategy": RESUME_STRATEGY if mode == "session" else "",
        "conversation_id": conv_id if mode == "session" else "",
        "agent": KIRO_AGENT or "",
    }
    if not ok:
        body["timed_out"] = result["timed_out"]
        body["stderr"] = result["stderr"]
    return body


def _background(payload: dict, prompt: str, session: str, channels: list, job_id: str) -> None:
    try:
        body = execute(payload, prompt, session, channels)
        body["response"] = body["response"][-2000:]
        print("[job] " + json.dumps({"job_id": job_id, **body}, ensure_ascii=False), flush=True)
    except Exception as e:  # never let a job die silently
        print(f"[job] {job_id} crashed: {type(e).__name__}: {e}", flush=True)


@app.post("/invocations")
def invocations():
    # force=True: EventBridge Scheduler / hand-rolled callers may not send
    # Content-Type: application/json, and Flask would then return None.
    payload = request.get_json(silent=True, force=True) or {}
    prompt = extract_prompt(payload)
    if not prompt:
        return (
            jsonify({"error": "empty prompt", "hint": 'send {"prompt": "<要巡检什么>"}'}),
            400,
        )

    if not os.environ.get("KIRO_API_KEY"):
        return jsonify({"error": "KIRO_API_KEY not set in runtime environment"}), 500

    agentcore_session = request.headers.get(
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id", ""
    )
    channels = pick_channels(payload)

    # {"async": true}: answer right away, run + deliver in the background.
    # /ping says HealthyBusy until the job ends, which keeps the session (and
    # its microVM) alive past the idle timeout for as long as the job runs.
    if _truthy(payload_flag(payload, "async")):
        job_id = f"job-{int(time.time() * 1000)}"
        _busy(+1)  # held until the thread has started its own run
        def runner():
            try:
                _background(payload, prompt, agentcore_session, channels, job_id)
            finally:
                _busy(-1)
        threading.Thread(target=runner, daemon=True).start()
        return jsonify({
            "status": "accepted",
            "job_id": job_id,
            "session_id": agentcore_session,
            "channels": channels,
            "response": f"巡检已在后台启动（{job_id}），结束后推送到：{', '.join(channels) or '（无渠道）'}",
        }), 200

    return jsonify(execute(payload, prompt, agentcore_session, channels)), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
