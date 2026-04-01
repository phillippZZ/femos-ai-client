"""
test_headless.py -- Skill creation integration test for FEMOS AI.

Tests whether the system can successfully create and validate a Python skill:
  1. create_skill -> validate_skill (simple skill)
  2. Corrupted context resilience (sends garbled history, verifies recovery)
  3. Bounce recovery (complex skill -- verifies hard-reset and recovery)

Usage:
    # Make sure the server is running first:
    #   cd femos-ai-server && python3 server.py
    #
    # Then (activate venv first):
    #   source venv/bin/activate
    #   cd femos-ai-client && python3 test_headless.py

Exit code 0 = all tests passed.  Non-zero = at least one failed.
"""

import argparse
import json
import sys
import threading
import time
import traceback
import uuid

import websockets
import asyncio


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

PASS = "\033[92m✔\033[0m"
FAIL = "\033[91m✘\033[0m"
INFO = "\033[94mℹ\033[0m"

_results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = ""):
    _results.append((name, ok, detail))
    icon = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {icon} {name}{suffix}")


def wait_for(predicate, poll: float = 0.05) -> bool:
    """Block indefinitely until predicate returns True."""
    while not predicate():
        time.sleep(poll)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Test runner — single shared WebSocket connection
# ─────────────────────────────────────────────────────────────────────────────

class HeadlessSession:
    def __init__(self, url: str):
        self.url = url
        self._ws = None
        self._loop: asyncio.AbstractEventLoop = None
        self._thread: threading.Thread = None

        # All received messages, appended in receive thread
        self._messages: list[dict] = []
        self._msg_lock = threading.Lock()
        self._connected = threading.Event()
        self._disconnected = threading.Event()

    # ── Connection management ──────────────────────────────────────────────

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=10):
            raise RuntimeError("Could not connect to server within 10 s — is it running?")

    def stop(self):
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        self._disconnected.wait(timeout=5)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect())

    async def _connect(self):
        try:
            async with websockets.connect(self.url) as ws:
                self._ws = ws
                self._connected.set()
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        with self._msg_lock:
                            self._messages.append(msg)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self._disconnected.set()

    # ── Send helpers ───────────────────────────────────────────────────────

    def send(self, payload: dict):
        if not self._ws or not self._loop:
            raise RuntimeError("Not connected")
        asyncio.run_coroutine_threadsafe(
            self._ws.send(json.dumps(payload)), self._loop
        ).result(timeout=5)

    def send_message(self, text: str) -> str:
        """Send a message, wait for task_started, then BLOCK until the task completes.
        Returns the task_id.  Blocking here prevents parallel LLM calls which
        compound latency and make timeouts unreliable."""
        task_id_holder: list = []
        mark = len(self._messages)
        self.send({"type": "message", "text": text})
        def _got_started():
            with self._msg_lock:
                for m in self._messages[mark:]:
                    if m.get("type") == "task_started":
                        task_id_holder.append(m.get("task_id", ""))
                        return True
            return False
        wait_for(_got_started)
        task_id = task_id_holder[0] if task_id_holder else ""
        if task_id:
            # Wait for the task to fully finish before returning so the caller
            # doesn't start a new LLM request while this one is still running.
            self.wait_for_completed(task_id)
        return task_id

    # ── Wait helpers ───────────────────────────────────────────────────────

    def wait_for_result(self, task_id: str) -> str | None:
        """Return the result text for task_id (non-blocking dict lookup).
        The task is guaranteed complete by the time send_message() returns."""
        with self._msg_lock:
            for m in self._messages:
                if m.get("type") == "result" and m.get("task_id") == task_id:
                    return m.get("text", "")
        return None

    def wait_for_completed(self, task_id: str) -> bool:
        """Block indefinitely until task_completed or task_failed arrives."""
        def _pred():
            with self._msg_lock:
                return any(
                    m.get("type") in ("task_completed", "task_failed")
                    and m.get("task_id") == task_id
                    for m in self._messages
                )
        return wait_for(_pred)

    def get_task_logs(self, task_id: str) -> list[str]:
        with self._msg_lock:
            return [
                m.get("text", "")
                for m in self._messages
                if m.get("type") == "task_log" and m.get("task_id") == task_id
            ]

    def messages_of_type(self, msg_type: str) -> list[dict]:
        with self._msg_lock:
            return [m for m in self._messages if m.get("type") == msg_type]

    def tool_calls_for_task(self, task_id: str = "") -> list[dict]:
        """Return tool_call messages (these are delegated to client)."""
        with self._msg_lock:
            return [
                m for m in self._messages
                if m.get("type") == "tool_call"
            ]

    def answer_tool_calls(self, default_result: str = "OK"):
        """Answer any pending tool_call messages so the task doesn't block."""
        calls = self.tool_calls_for_task()
        for tc in calls:
            call_id = tc.get("call_id")
            if not call_id:
                continue
            # Check if we already answered it
            with self._msg_lock:
                already = any(
                    m.get("type") == "tool_result" and m.get("call_id") == call_id
                    for m in self._messages
                )
            if not already:
                self.send({
                    "type": "tool_result",
                    "call_id": call_id,
                    "name": tc.get("name", ""),
                    "content": default_result,
                })

    def answer_tool_calls_real(self, skills: dict, fallback: str = "OK"):
        """Execute tool_call messages using the real client skill functions.
        Unknown tools fall back to `fallback`.  Already-answered calls are skipped."""
        for tc in self.tool_calls_for_task():
            call_id = tc.get("call_id")
            if not call_id:
                continue
            with self._msg_lock:
                already = any(
                    m.get("type") == "tool_result" and m.get("call_id") == call_id
                    for m in self._messages
                )
            if already:
                continue
            func_name = tc.get("name", "")
            args = tc.get("args") or {}
            if func_name in skills:
                try:
                    result = str(skills[func_name](**args))
                except Exception as e:
                    result = f"SKILL_ERROR: {type(e).__name__}: {e}"
            else:
                result = fallback
            self.send({
                "type": "tool_result",
                "call_id": call_id,
                "name": func_name,
                "content": result,
            })

    def send_message_with_tools(self, text: str, skills: dict) -> str:
        """Like send_message but continuously answers incoming tool_call messages
        in a background thread while blocking for task_completed.
        Use this for any request that will delegate tool calls to the client."""
        stop_ev = threading.Event()

        def _answer_loop():
            while not stop_ev.wait(timeout=0.3):
                self.answer_tool_calls_real(skills)
            # One final sweep after the task finishes
            self.answer_tool_calls_real(skills)

        t = threading.Thread(target=_answer_loop, daemon=True)
        t.start()
        try:
            task_id = self.send_message(text)  # blocks until task_completed
        finally:
            stop_ev.set()
            t.join(timeout=2)
        return task_id


# ─────────────────────────────────────────────────────────────────────────────
# Individual tests
# ─────────────────────────────────────────────────────────────────────────────

def test_create_and_validate_skill(sess: HeadlessSession, client_skills: dict):
    print(f"\n{INFO} Test 4+5+6: create_skill → validate_skill → call skill")
    import os
    skill_name = f"test_ping_{int(time.time())}"
    # __file__ is inside femos-ai-client/, so skills/ is a direct sibling
    skill_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", skill_name)
    skill_file = os.path.join(skill_dir, "__init__.py")

    task_id = sess.send_message_with_tools(
        f"Create a skill named '{skill_name}' that takes a parameter 'msg: str' and returns 'PONG: ' + msg. "
        f"After creating it, validate it with test_args={{\"msg\": \"hello\"}} and report the result.",
        client_skills,
    )
    if not task_id:
        report("skill task started", False)
        return

    result = sess.wait_for_result(task_id)
    logs = sess.get_task_logs(task_id)

    validated = any("VALIDATION" in l.upper() for l in logs) or (result and "VALIDATION" in result.upper())
    file_exists = os.path.exists(skill_file)
    report("skill folder created", os.path.isdir(skill_dir),
           skill_dir if os.path.isdir(skill_dir) else f"NOT FOUND: {skill_dir}")
    report("skill __init__.py written", file_exists,
           skill_file if file_exists else f"NOT FOUND: {skill_file}")
    report("validation attempted", validated, (result or "no result")[:80])
    if file_exists:
        with open(skill_file) as _f:
            src = _f.read()
        report("skill file contains SKILL_FN", "SKILL_FN" in src, f"{len(src)} chars")


def test_corrupted_context(sess: HeadlessSession, client_skills: dict, tools_config: list):
    """
    Resilience test: corrupt the server's history mid-session, then verify
    the system can still complete a skill creation task.

    Simulates: user manually edited the history file, a storage error truncated
    it, or the client crashed and sent back garbled state on reconnect.

    Three corruption scenarios applied in sequence:
      A) Truncated history — only the last 2 messages kept, rest lost.
      B) Garbage injected — non-dict entries and broken role fields.
      C) Wiped history   — clear_history message empties the server state.

    After each corruption the system is asked to create a small skill.  A task
    completing at all (task_completed received) counts as a pass.
    """
    print(f"\n{INFO} Test 2: Corrupted context resilience")
    import os

    def _skill_name():
        return f"ctx_test_{int(time.time())}"

    def _run_skill_task(label: str) -> bool:
        """Send a create_skill request and return True if task_completed."""
        name = _skill_name()
        task_id = sess.send_message_with_tools(
            f"Create a skill named '{name}' that takes no arguments and returns the string 'OK'.",
            client_skills,
        )
        if not task_id:
            report(f"{label}: task started", False)
            return False
        with sess._msg_lock:
            completed = any(
                m.get("type") == "task_completed" and m.get("task_id") == task_id
                for m in sess._messages
            )
        report(f"{label}: task completed after corruption", completed,
               task_id[:8] if completed else "task_failed or no response")
        return completed

    # ── Scenario A: truncated history ─────────────────────────────────────
    # Grab the history snapshot from the last completed task and send back
    # only the final 2 entries, simulating a truncated save file.
    with sess._msg_lock:
        snapshots = [m for m in sess._messages if m.get("type") == "history_snapshot"]
    if snapshots:
        full_history = snapshots[-1].get("history", [])
        truncated = full_history[-2:] if len(full_history) >= 2 else full_history
    else:
        truncated = []
    # Re-hello with the truncated history (simulates client restoring from a partial save)
    sess.send({"type": "hello", "client_id": f"test-corrupt-a-{uuid.uuid4()}", "history": truncated})
    time.sleep(0.3)
    _run_skill_task("truncated history")

    # ── Scenario B: garbage injected into history ──────────────────────────
    garbage_history = [
        "not a dict at all",
        {"role": None, "content": None},
        {"role": "user"},           # missing content
        {"content": "orphan"},      # missing role
        {"role": "INVALID_ROLE", "content": "bad"},
    ]
    sess.send({"type": "hello", "client_id": f"test-corrupt-b-{uuid.uuid4()}", "history": garbage_history})
    time.sleep(0.3)
    _run_skill_task("garbage history")

    # ── Scenario C: wiped history (clear_history message) ─────────────────
    sess.send({"type": "clear_history"})
    time.sleep(0.1)
    _run_skill_task("wiped history")

    # ── Scenario D: workspace_tasks with phantom skills ───────────────────
    # Simulates: a previous session wrote task progress claiming skills were
    # created, but those skills no longer exist on disk (user deleted them,
    # crash, or manual edit of the task file).
    phantom_tasks = [
        {
            "task_id": "phantom-task-1",
            "title": "Build data pipeline skill",
            "status": "in_progress",
            "current_step": 2,
            "total_steps": 3,
            "plan_steps": ["design schema", "create fetch_data skill", "create transform_data skill"],
            "artifacts": {
                "0": ["fetch_data"],     # claimed created, but file doesn't exist
                "1": ["transform_data"], # same
            },
            "needs_verification": True,
            "notes": "fetch_data and transform_data were created last session",
        }
    ]
    sess.send({"type": "register_skills", "skills": tools_config, "workspace_tasks": phantom_tasks})
    time.sleep(0.3)
    _run_skill_task("phantom workspace skills (needs_verification=True)")

    # ── Scenario E: workspace_tasks with corrupt/garbage entries ──────────
    # Simulates: task storage file was manually edited or partially corrupted.
    garbage_tasks = [
        "not a dict",
        None,
        {"task_id": None, "title": None, "status": "in_progress"},
        {"task_id": "bad-1", "current_step": "FIVE", "total_steps": "TEN"},
        {"task_id": "bad-2", "title": "half-done skill", "status": "in_progress",
         "artifacts": "should be a dict not a string"},
    ]
    sess.send({"type": "register_skills", "skills": tools_config, "workspace_tasks": garbage_tasks})
    time.sleep(0.3)
    _run_skill_task("garbage workspace_tasks")

    # Restore clean workspace state for subsequent tests
    sess.send({"type": "register_skills", "skills": tools_config, "workspace_tasks": []})
    time.sleep(0.3)


def test_bounce_recovery(sess: HeadlessSession, client_skills: dict):
    """
    Test the bounce-counter + hard-reset mechanism.

    Two acceptable outcomes:
      A) Model uses the two-step pattern correctly → task completes with 0 bounces (PASS — ideal).
      B) Model produces malformed JSON → bounce counter increments → hard-reset fires → task
         eventually completes (PASS — recovery worked).

    The only real failure is the task hanging forever or erroring silently.
    """
    print(f"\n{INFO} Test 9: Bounce counter + hard-reset (observational)")
    task_id = sess.send_message_with_tools(
        "Write a Python skill called 'big_test_skill' that has a detailed class with 10 methods. "
        "Use the two-step pattern: workspace_files to write the code, then create_skill to register it.",
        client_skills,
    )
    if not task_id:
        report("bounce test task started", False)
        return

    # send_message_with_tools blocks until task_completed, answering tool calls throughout.
    # By the time this returns the task is done — no separate polling loop needed.
    with sess._msg_lock:
        task_completed = any(
            m.get("type") == "task_completed" and m.get("task_id") == task_id
            for m in sess._messages
        )
        task_failed = any(
            m.get("type") == "task_failed" and m.get("task_id") == task_id
            for m in sess._messages
        )

    logs = sess.get_task_logs(task_id)
    bounce_logs = [l for l in logs if "Bounce" in l or "bounce" in l or "reset" in l.lower()]

    if bounce_logs:
        # Outcome B: bounces happened, check hard-reset fired and task recovered
        hard_reset_fired = any("reset" in l.lower() or "Reset" in l for l in bounce_logs)
        report(f"bounce+reset recovery ({len(bounce_logs)} bounce events)",
               hard_reset_fired or task_completed,
               bounce_logs[-1][:80])
    else:
        # Outcome A: no bounces at all — model followed the two-step pattern correctly
        report("no bounce needed — model used correct two-step pattern",
               task_completed,
               "task completed cleanly" if task_completed else "task timed out or failed")


# ─────────────────────────────────────────────────────────────────────────────
# Real client tool setup
# ─────────────────────────────────────────────────────────────────────────────


def test_transaction_manager(sess: HeadlessSession, client_skills: dict):
    """
    Multi-skill project test: ask the model to build a minimal transaction
    manager system consisting of two skills:

      1. txn_ledger  — stores and retrieves an in-memory list of transactions
                       (add, list, clear operations)
      2. txn_summary — reads the ledger and returns total / count / average

    Checks:
      - Both skill folders exist under skills/
      - Both __init__.py files contain SKILL_FN
      - txn_ledger(action='add', amount=50, description='groceries') returns ok
      - txn_summary() returns a numeric total
      - No workspace staging files remain after completion
    """
    print(f"\n{INFO} Test: Transaction manager (multi-skill project)")
    import os

    skills_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    ws_root     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")

    task_id = sess.send_message_with_tools(
        "Build a minimal transaction manager system as two separate skills:\n"
        "1. A skill named 'txn_ledger' that manages an in-memory list of transactions. "
        "It takes action='add'|'list'|'clear', and for 'add' also amount (float) and description (str). "
        "'add' appends {amount, description} and returns 'added'. "
        "'list' returns the JSON-serialised list. 'clear' empties it and returns 'cleared'.\n"
        "2. A skill named 'txn_summary' that reads txn_ledger(action='list') and returns a string: "
        "'count=N total=X.XX avg=Y.YY'.\n"
        "Use the two-step pattern (workspace_files then create_skill) for each skill. "
        "After creating both, call txn_ledger(action='add', amount=42.0, description='test') "
        "and then txn_summary() and report the results.",
        client_skills,
    )
    if not task_id:
        report("txn task started", False)
        return

    result = sess.wait_for_result(task_id)

    # ── Structural checks ─────────────────────────────────────────────────
    for skill_name in ("txn_ledger", "txn_summary"):
        skill_dir  = os.path.join(skills_root, skill_name)
        skill_init = os.path.join(skill_dir, "__init__.py")
        dir_ok  = os.path.isdir(skill_dir)
        file_ok = os.path.isfile(skill_init)
        report(f"{skill_name}: folder created", dir_ok,
               skill_dir if dir_ok else f"NOT FOUND: {skill_dir}")
        report(f"{skill_name}: __init__.py written", file_ok,
               skill_init if file_ok else f"NOT FOUND: {skill_init}")
        if file_ok:
            with open(skill_init) as _f:
                src = _f.read()
            report(f"{skill_name}: contains SKILL_FN", "SKILL_FN" in src, f"{len(src)} chars")

    # ── Runtime checks — call the skills directly ─────────────────────────
    # Reload so the test process sees the freshly created skills
    try:
        from core.tools import reload_skills, SKILLS
        reload_skills()
        if "txn_ledger" in SKILLS and "txn_summary" in SKILLS:
            ledger_add = str(SKILLS["txn_ledger"](action="add", amount=99.0, description="txn_test"))
            report("txn_ledger: add returns 'added'", "add" in ledger_add.lower(), ledger_add[:60])
            summary = str(SKILLS["txn_summary"]())
            report("txn_summary: returns numeric result",
                   any(c.isdigit() for c in summary), summary[:80])
        else:
            missing = [n for n in ("txn_ledger", "txn_summary") if n not in SKILLS]
            report("txn skills callable", False, f"not in SKILLS: {missing}")
    except Exception as e:
        report("txn runtime call", False, str(e)[:80])

    # ── No staging files left in workspace/ ────────────────────────────────
    stale = [f for f in os.listdir(ws_root)
             if f.endswith(".py") and f in ("txn_ledger.py", "txn_summary.py")]
    report("no staging files left in workspace/", not stale,
           f"found: {stale}" if stale else "clean")

def _setup_real_tools() -> dict:
    """
    Load the real client-side builtin skill functions so the test can execute
    tool_call messages with actual implementations instead of mocks.

    Returns a dict {function_name: callable} mirroring what WsClient.skills contains.
    """
    import sys, os
    # __file__ is inside femos-ai-client/ — add it to sys.path so core.* imports work
    # when the script is run from a different cwd (e.g. the repo root).
    client_dir = os.path.dirname(os.path.abspath(__file__))
    if client_dir not in sys.path:
        sys.path.insert(0, client_dir)
    try:
        from core.tools import _load_builtins, reload_skills, SKILLS, TOOLS_CONFIG, _BUILTIN_SKILLS
        if not _BUILTIN_SKILLS:   # idempotent — only load once
            _load_builtins()
        reload_skills()           # also loads any existing user skills
        skills = dict(SKILLS)
        tools_config = list(TOOLS_CONFIG)
        # Headless override: ask_user would block on stdin — auto-confirm everything.
        skills["ask_user"] = lambda question="", title="", default="": "yes"
        print(f"  {INFO} Loaded {len(skills)} real client tools: {', '.join(sorted(skills)[:8])}{'…' if len(skills) > 8 else ''}")
        return skills, tools_config
    except Exception as e:
        print(f"  {FAIL} Could not load real client tools: {e}")
        return {}, []


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FEMOS skill creation integration test")
    parser.add_argument("--url", default="ws://localhost:8765", help="Server WebSocket URL")
    args = parser.parse_args()

    print(f"FEMOS Skill Creation Test")
    print(f"  Server : {args.url}")
    print("=" * 50)

    import sys, os
    client_dir = os.path.dirname(os.path.abspath(__file__))
    if client_dir not in sys.path:
        sys.path.insert(0, client_dir)

    print(f"\n{INFO} Loading real client tools…")
    client_skills, tools_config = _setup_real_tools()

    sess = HeadlessSession(args.url)
    try:
        sess.start()
    except RuntimeError as e:
        print(f"\n  {FAIL} Could not connect: {e}")
        print("  Make sure femos-ai-server/server.py is running.")
        _results.append(("server connection", False, str(e)))
    else:
        try:
            # Identify and register full toolset before any LLM tests.
            sess.send({"type": "hello", "client_id": f"test-{uuid.uuid4()}", "history": []})
            time.sleep(0.3)
            sess.send({"type": "register_skills", "skills": tools_config, "workspace_tasks": []})
            time.sleep(0.3)
            test_create_and_validate_skill(sess, client_skills)
            test_corrupted_context(sess, client_skills, tools_config)
            test_bounce_recovery(sess, client_skills)
            test_transaction_manager(sess, client_skills)
        except Exception as e:
            traceback.print_exc()
            _results.append(("unexpected error", False, str(e)))
        finally:
            sess.stop()

    # Summary
    print("\n" + "=" * 50)
    passed = sum(1 for _, ok, _ in _results if ok)
    total  = len(_results)
    print(f"Results: {passed}/{total} passed")
    for name, ok, detail in _results:
        icon = PASS if ok else FAIL
        suffix = f"  — {detail}" if detail else ""
        print(f"  {icon} {name}{suffix}")

    failed = [n for n, ok, _ in _results if not ok]
    if failed:
        print(f"\nFailed: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("\nAll tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
