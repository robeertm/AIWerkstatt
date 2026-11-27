"""The Claude run adapter (agent-runner/providers/claude_code.py): how a run is started and how its
stream is read. No CLI, no network: argv is built and canned stream-json lines are parsed.

What matters for the outside world: the secret never reaches argv, the subagent guard is wired in,
a real usage limit is told apart from a transient API error, and garbage lines never crash a run.

Run: cd backend && python3 -m unittest discover -s tests"""
import json
import os
import sys
import unittest
from unittest import mock

import importlib.util  # noqa: E402

# The backend has its own `providers` package; load the runner's one under a private name.
_PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent-runner", "providers"))
_spec = importlib.util.spec_from_file_location("aiw_runner_providers", os.path.join(_PKG, "__init__.py"),
                                               submodule_search_locations=[_PKG])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["aiw_runner_providers"] = _mod
_spec.loader.exec_module(_mod)
from aiw_runner_providers import claude_code as cc  # noqa: E402
from aiw_runner_providers.base import RunContext  # noqa: E402


def line(**o):
    return json.dumps(o)


class Argv(unittest.TestCase):
    def argv(self, **kw):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "TEST-VALUE-x", "CLAUDE_CODE_OAUTH_TOKEN": "TEST-VALUE-y"}):
            return cc.ClaudeAdapter().build_argv(RunContext(**kw))

    def test_stream_mode_model_and_permissions(self):
        a = self.argv(model="sonnet")
        self.assertEqual(a[:2], ["claude", "-p"])
        self.assertEqual(a[a.index("--model") + 1], "sonnet")
        self.assertEqual(a[a.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("stream-json", a)

    def test_no_secret_in_argv(self):
        self.assertNotIn("TEST-VALUE", " ".join(self.argv(model="sonnet")))

    def test_effort_and_resume_only_when_valid(self):
        a = self.argv(model="m", effort="HIGH", resume_id="sess-1")
        self.assertEqual(a[a.index("--effort") + 1], "high")
        self.assertEqual(a[a.index("--resume") + 1], "sess-1")
        a = self.argv(model="m", effort="bogus")
        self.assertNotIn("--effort", a)
        self.assertNotIn("--resume", a)

    def test_agent_tool_always_allowed_and_custom_tools_kept(self):
        a = self.argv(model="m", allowed_tools="Read,Bash")
        self.assertEqual(a[a.index("--allowedTools") + 1], "Read,Bash,Agent")
        a = self.argv(model="m", allowed_tools="Read,Agent")
        self.assertEqual(a[a.index("--allowedTools") + 1], "Read,Agent")

    def test_rules_forbid_background_jobs(self):
        a = self.argv(model="m")
        rules = a[a.index("--append-system-prompt") + 1]
        self.assertIn("NEVER use background processes", rules)

    def test_guard_settings_hook_the_agent_tool_with_the_run_model(self):
        a = self.argv(model="sonnet")
        path = a[a.index("--settings") + 1]
        with open(path) as fh:
            cfg = json.load(fh)
        hook = cfg["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "^(Agent|Task)$")
        cmd = hook["hooks"][0]["command"]
        self.assertIn("--model sonnet", cmd)
        self.assertIn("--max 2", cmd)

    def test_guard_missing_means_no_settings_flag(self):
        with mock.patch.object(cc, "_GUARD", "/nonexistent/guard.py"):
            a = cc.ClaudeAdapter().build_argv(RunContext(model="m"))
        self.assertNotIn("--settings", a)

    def test_user_message_is_one_json_line(self):
        raw = cc.ClaudeAdapter().encode_user_message("hällo\nwelt")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw)["message"]["content"], "hällo\nwelt")


class ParseResult(unittest.TestCase):
    def parse(self, **o):
        return cc.ClaudeAdapter().parse_line(line(**o))

    def result(self, **o):
        out = self.parse(type="result", **o)
        self.assertEqual(len(out), 1)
        return out[0]

    def test_success(self):
        r = self.result(result="done", usage={"output_tokens": 12}, total_cost_usd=0.5)
        self.assertEqual((r["error"], r["limited"], r["transient"], r["api_error"]), (False, False, False, False))
        self.assertEqual((r["text"], r["out_tokens"]), ("done", 12))

    def test_real_usage_limit_is_limited_not_api_error(self):
        r = self.result(is_error=True, api_error_status=429, rate_limit_info={"resetsAt": 1900000000})
        self.assertTrue(r["limited"])
        self.assertEqual(r["reset"], 1900000000)
        self.assertFalse(r["api_error"])
        self.assertFalse(r["transient"])

    def test_transient_api_error_is_not_a_limit(self):
        r = self.result(is_error=True, terminal_reason="api_error", api_error_status=529, result="Overloaded")
        self.assertFalse(r["limited"])
        self.assertTrue(r["api_error"])
        self.assertEqual(r["api_status"], 529)

    def test_aborted_stream_is_transient(self):
        r = self.result(is_error=True, subtype="error_during_execution", terminal_reason="aborted_streaming")
        self.assertTrue(r["transient"])
        self.assertFalse(r["limited"])

    def test_plain_error_is_failed_not_retryable(self):
        r = self.result(is_error=True, subtype="error_max_turns", result="too many turns")
        self.assertTrue(r["error"])
        self.assertFalse(r["transient"] or r["api_error"] or r["limited"])

    def test_bad_numbers_do_not_crash(self):
        r = self.result(usage={"output_tokens": "many"}, total_cost_usd="free", rate_limit_info={"resetsAt": "soon"})
        self.assertEqual((r["out_tokens"], r["cost"], r["reset"]), (0, 0.0, 0))

    def test_rate_limit_event_only_when_rejected(self):
        a = cc.ClaudeAdapter()
        ok = a.parse_line(line(type="rate_limit_event", rate_limit_info={"status": "allowed"}))
        bad = a.parse_line(line(type="rate_limit_event", rate_limit_info={"status": "rejected", "resetsAt": 5}))
        self.assertEqual(ok, [])
        self.assertEqual(bad, [{"kind": "limit", "reset": 5}])


class ParseStream(unittest.TestCase):
    def test_garbage_never_raises(self):
        a = cc.ClaudeAdapter()
        for raw in ("", "   ", "not json", "[1,2]", "null", '{"type": "assistant", "message": 5}',
                    '{"type": "user", "message": "x"}', '{"type": "system", "tasks": 3}'):
            self.assertIsInstance(a.parse_line(raw), list)

    def test_session_id_is_reported(self):
        out = cc.ClaudeAdapter().parse_line(line(type="system", session_id="abc"))
        self.assertEqual(out, [{"kind": "session", "id": "abc"}])

    def test_context_bar_counts_main_conversation_only(self):
        a = cc.ClaudeAdapter()
        usage = {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}
        main = a.parse_line(line(type="assistant", message={"usage": usage, "content": []}))
        sub = a.parse_line(line(type="assistant", parent_tool_use_id="t1", message={"usage": usage, "content": []}))
        self.assertEqual(main, [{"kind": "ctx", "tokens": 35, "window": None}])
        self.assertEqual(sub, [])

    def test_activities_and_subagent_flag(self):
        a = cc.ClaudeAdapter()
        msg = {"content": [{"type": "text", "text": "hello"},
                           {"type": "tool_use", "name": "Bash", "input": {"command": "ls\nmore"}}]}
        out = a.parse_line(line(type="assistant", parent_tool_use_id="t1", message=msg))
        acts = [e for e in out if e["kind"] == "activity"]
        self.assertEqual([e["act"] for e in acts], ["say", "tool"])
        self.assertEqual(acts[1]["text"], "$ ls")
        self.assertIn("more", acts[1]["full"])
        self.assertTrue(all(e["top"] is False for e in acts))

    def test_long_text_is_clipped_in_short_form_only(self):
        a = cc.ClaudeAdapter()
        out = a.parse_line(line(type="assistant", message={"content": [{"type": "text", "text": "w " * 400}]}))
        ev = out[0]
        self.assertTrue(ev["text"].endswith("…"))
        self.assertLessEqual(len(ev["text"]), cc._MAX_ACT + 2)
        self.assertGreater(len(ev["full"]), len(ev["text"]))

    def test_plan_follows_task_calls_and_can_be_seeded(self):
        a = cc.ClaudeAdapter()

        def call(name, inp):
            return a.parse_line(line(type="assistant", message={"content": [
                {"type": "tool_use", "name": name, "input": inp}]}))[0]
        call("TaskCreate", {"subject": "one", "activeForm": "doing one"})
        call("TaskCreate", {"subject": "two"})
        ev = call("TaskUpdate", {"taskId": "1", "status": "in_progress"})
        self.assertEqual(ev["text"], "Plan: 0/2 done — now: doing one")
        ev = call("TaskUpdate", {"taskId": "1", "status": "completed"})
        self.assertEqual(ev["plan"]["1"]["status"], "completed")
        b = cc.ClaudeAdapter()
        b.seed_plan({"1": {"subject": "x", "status": "completed"}, "bad": 3})
        self.assertEqual(list(b._tasks), ["1"])

    def test_todowrite_replaces_the_list(self):
        a = cc.ClaudeAdapter()
        ev = a.parse_line(line(type="assistant", message={"content": [{"type": "tool_use", "name": "TodoWrite", "input": {
            "todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress", "activeForm": "bb"}]}}]}))[0]
        self.assertEqual(ev["text"], "Plan: 1/2 done — now: bb")

    def test_tool_result_keeps_full_text(self):
        out = cc.ClaudeAdapter().parse_line(line(type="user", message={"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "l1\nl2\nl3"}]}]}))
        self.assertEqual(out[0]["act"], "result")
        self.assertIn("(3 lines)", out[0]["text"])
        self.assertEqual(out[0]["full"], "l1\nl2\nl3")

    def test_background_work_events(self):
        a = cc.ClaudeAdapter()
        full = a.parse_line(line(type="system", subtype="background_tasks_changed",
                                 tasks=[{"task_id": 7, "description": "d", "task_type": "bash", "status": "running"}, {}]))
        self.assertEqual(full, [{"kind": "background", "tasks": [
            {"id": "7", "description": "d", "type": "bash", "status": "running"}]}])
        ended = a.parse_line(line(type="system", subtype="task_updated", task_id="7", patch={"status": "killed"}))
        self.assertEqual(ended, [{"kind": "background", "ended": "7"}])
        started = a.parse_line(line(type="system", subtype="task_started", task_id="8", is_backgrounded=True))
        self.assertEqual(started[0]["started"]["id"], "8")
        self.assertEqual(a.parse_line(line(type="system", subtype="task_notification", task_id="8")),
                         [{"kind": "background", "ended": "8"}])

    def test_context_window_defaults(self):
        a = cc.ClaudeAdapter()
        self.assertEqual(a.default_context_window("claude-opus-4-8"), 1_000_000)
        self.assertEqual(a.default_context_window("sonnet"), 200_000)


if __name__ == "__main__":
    unittest.main()
