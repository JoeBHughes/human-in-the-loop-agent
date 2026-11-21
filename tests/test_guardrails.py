"""Tests for each guardrail. They use scripted model replies, so they need no API key."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

FACTS = str(pathlib.Path(__file__).resolve().parent.parent / "facts.json")


def run_main(tmp_path, answer, extra=(), client=None):
    argv = ["--dry-run", "--facts", FACTS, "--out", str(tmp_path / "out"), "--logs", str(tmp_path / "logs"), *extra]
    return agent.main(argv, ask=lambda _prompt: answer, client=client)


def test_lookup_returns_only_approved_facts():
    facts = agent.load_facts(FACTS)
    hits = agent.lookup_facts(facts, "offline sync")["facts"]
    assert [f["id"] for f in hits] == ["F02"]
    miss = agent.lookup_facts(facts, "Android pricing")
    assert miss["facts"] == [] and "Do not state" in miss["note"]


def test_checker_flag_sends_draft_back_once(tmp_path):
    client = agent.dry_run_client()
    assert run_main(tmp_path, "approve", client=client) == 0
    roles = ["checker" if "tool_choice" in r else "writer" for r in client.requests]
    assert roles == ["writer", "writer", "checker", "writer", "checker"]
    saved = next((tmp_path / "out" / "approved").iterdir()).read_text()
    assert "Android" not in saved


def test_unknown_fact_id_fails_even_if_checker_agrees(tmp_path):
    facts = agent.load_facts(FACTS)
    client = agent.ScriptedClient([], [agent._reply([agent._tool("c1", "report_claims", {"claims": [
        {"claim": "Ships March 3", "status": "supported", "fact_id": "F01"}]})])])
    run = agent.Run(client, max_steps=5, log_path=tmp_path / "log.jsonl")
    check = agent.check_draft(run, facts, "Ships March 3.", ["F01", "F99"], "checker-model")
    assert not check["passed"] and check["unknown_ids"] == ["F99"]


def test_step_limit_stops_run_and_saves_nothing(tmp_path):
    assert run_main(tmp_path, "approve", extra=["--max-steps", "3"]) == 2
    assert not (tmp_path / "out").exists()


def test_gate_rejects_anything_but_approve(tmp_path):
    for n, answer in enumerate(["yes", "", "y", "approved?"]):
        assert run_main(tmp_path / str(n), answer) == 1


def test_gate_rejects_when_no_one_answers():
    def no_input(_prompt):
        raise EOFError
    result = {"draft": "x", "fact_ids": [], "revisions": 0,
              "check": {"passed": True, "claims": [], "unsupported": [], "unknown_ids": []}}
    assert agent.approval_gate(result, ask=no_input) is False


def test_every_step_is_logged(tmp_path):
    run_main(tmp_path, "approve")
    log = next((tmp_path / "logs").iterdir()).read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in log]
    assert kinds[0] == "start" and kinds[-1] == "decision"
    assert kinds.count("model_call") == 5
