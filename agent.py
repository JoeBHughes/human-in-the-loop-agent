#!/usr/bin/env python3
"""Draft content from approved facts, check it, and hold it for human approval.

Guardrails, in the order a run meets them:
  1. Approved facts only. The writer model reads facts only through the lookup_facts tool.
  2. Claim check. A second model compares each claim in the draft with the approved facts.
     An automated script also confirms that every cited fact ID exists.
  3. Step limit. The run stops after --max-steps model calls and saves nothing.
  4. Logging. An automated logger writes every model call and tool call to logs/.
  5. Approval gate. A human reviewer must type "approve" before the draft is saved.

Run with --dry-run to see the whole flow with scripted model replies and no API key.
"""
import argparse
import datetime
import json
import pathlib
import re
import sys
from types import SimpleNamespace

WRITER_MODEL = "claude-sonnet-5-5"
CHECKER_MODEL = "claude-haiku-5-5"
DEFAULT_TASK = "Write a three-sentence announcement of the Lumen Notes 2.0 release for the product blog."

WRITER_SYSTEM = (
    "You write short product announcements. Use only facts returned by the lookup_facts tool. "
    "Do not add numbers, dates, platforms, features or claims that the facts do not state. "
    "When the draft is ready, call submit_draft with the draft and the IDs of the facts you used."
)

CHECKER_SYSTEM = (
    "You are a fact checker. Split the draft into individual factual claims. "
    "Mark a claim supported only if one approved fact states it. "
    "Mark it unsupported if no approved fact states it, even if it sounds true. "
    "Report every claim with the report_claims tool."
)

WRITER_TOOLS = [
    {
        "name": "lookup_facts",
        "description": "Search the approved facts. Returns matching facts with their IDs. These are the only facts you may use.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Words to search for"}},
            "required": ["query"],
        },
    },
    {
        "name": "submit_draft",
        "description": "Submit the finished draft for a claim check and human review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "draft": {"type": "string"},
                "fact_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs of the facts the draft uses"},
            },
            "required": ["draft", "fact_ids"],
        },
    },
]

CHECKER_TOOL = {
    "name": "report_claims",
    "description": "Report each claim in the draft and whether an approved fact supports it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "status": {"type": "string", "enum": ["supported", "unsupported"]},
                        "fact_id": {"type": "string", "description": "ID of the supporting fact, or empty"},
                    },
                    "required": ["claim", "status", "fact_id"],
                },
            }
        },
        "required": ["claims"],
    },
}


class StepLimitReached(Exception):
    pass


# Guardrail 1: approved facts only

def load_facts(path):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return data["facts"]


def lookup_facts(facts, query, limit=8):
    """Return approved facts that contain any query word of four or more letters."""
    words = [w for w in re.findall(r"[a-z0-9.]+", query.lower()) if len(w) >= 4]
    hits = [f for f in facts if any(w in f["text"].lower() for w in words)]
    if not hits:
        return {"facts": [], "note": "No approved fact matches. Do not state anything about this."}
    return {"facts": hits[:limit]}


# Guardrails 3 and 4: step limit and logging

def block_to_dict(block):
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return dict(vars(block))


class Run:
    """Wraps every model call so it counts toward the step limit and lands in the log."""

    def __init__(self, client, max_steps, log_path):
        self.client = client
        self.max_steps = max_steps
        self.steps = 0
        self.log_path = pathlib.Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind, **data):
        entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def call(self, role, **request):
        if self.steps >= self.max_steps:
            self.log("step_limit", steps=self.steps)
            raise StepLimitReached(self.steps)
        self.steps += 1
        response = self.client.messages.create(**request)
        usage = getattr(response, "usage", None)
        self.log(
            "model_call",
            step=self.steps,
            role=role,
            model=request["model"],
            stop_reason=response.stop_reason,
            content=[block_to_dict(b) for b in response.content],
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        return response


# Guardrail 2: claim check

def check_draft(run, facts, draft, fact_ids, checker_model):
    fact_list = "\n".join(f"{f['id']}: {f['text']}" for f in facts)
    response = run.call(
        "checker",
        model=checker_model,
        max_tokens=2048,
        system=CHECKER_SYSTEM,
        tools=[CHECKER_TOOL],
        tool_choice={"type": "tool", "name": "report_claims"},
        messages=[{"role": "user", "content": f"Approved facts:\n{fact_list}\n\nDraft:\n{draft}"}],
    )
    claims = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_claims":
            claims = block.input.get("claims", [])

    # An automated script checks the cited IDs. This part doesn't depend on a model.
    known = {f["id"] for f in facts}
    unknown_ids = [i for i in fact_ids if i not in known]
    unsupported = [c for c in claims if c.get("status") != "supported"]
    passed = bool(claims) and not unsupported and not unknown_ids
    run.log("claim_check", passed=passed, claims=claims, unknown_fact_ids=unknown_ids)
    return {"passed": passed, "claims": claims, "unsupported": unsupported, "unknown_ids": unknown_ids}


def feedback(check):
    lines = ["The claim check failed. Fix these problems, then call submit_draft again."]
    for c in check["unsupported"]:
        lines.append(f"- No approved fact supports: {c['claim']}")
    for i in check["unknown_ids"]:
        lines.append(f"- Fact ID {i} doesn't exist.")
    if not check["claims"]:
        lines.append("- The checker found no claims to verify.")
    return "\n".join(lines)


def tool_result(tool_use_id, content):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


# The agent loop

def run_agent(run, facts, task, writer_model, checker_model, max_revisions):
    messages = [{"role": "user", "content": task}]
    revisions = 0
    while True:
        response = run.call(
            "writer",
            model=writer_model,
            max_tokens=2048,
            system=WRITER_SYSTEM,
            tools=WRITER_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        results = []
        submitted = None
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "lookup_facts":
                output = lookup_facts(facts, block.input.get("query", ""))
                run.log("tool_call", name="lookup_facts", input=block.input, output=output)
                print(f"Step {run.steps}: writer looked up \"{block.input.get('query', '')}\" and got {len(output['facts'])} approved facts")
                results.append(tool_result(block.id, json.dumps(output)))
            elif block.name == "submit_draft":
                submitted = block
            else:
                results.append(tool_result(block.id, f"Unknown tool {block.name}."))

        if submitted is not None:
            draft = submitted.input.get("draft", "")
            fact_ids = submitted.input.get("fact_ids", [])
            run.log("tool_call", name="submit_draft", input=submitted.input)
            print(f"Step {run.steps}: writer submitted a draft")
            check = check_draft(run, facts, draft, fact_ids, checker_model)
            flagged = len(check["unsupported"]) + len(check["unknown_ids"])
            if check["passed"]:
                print(f"Step {run.steps}: checker found {len(check['claims'])} claims, all supported")
            else:
                print(f"Step {run.steps}: checker flagged {flagged} of {len(check['claims'])} claims")
            if check["passed"] or revisions >= max_revisions:
                return {"draft": draft, "fact_ids": fact_ids, "check": check, "revisions": revisions}
            revisions += 1
            print("        Sending the flagged claims back to the writer")
            results.append(tool_result(submitted.id, feedback(check)))

        if results:
            messages.append({"role": "user", "content": results})
        else:
            messages.append({"role": "user", "content": "Call submit_draft with your draft."})


# Guardrail 5: approval gate

def show_for_review(result):
    print("\n=== Draft for review ===\n")
    print(result["draft"])
    print(f"\nFacts cited: {', '.join(result['fact_ids']) or 'none'}")
    print(f"Revisions: {result['revisions']}")
    print("\nClaim check:")
    for c in result["check"]["claims"]:
        mark = "OK  " if c.get("status") == "supported" else "FLAG"
        print(f"  {mark} {c['claim']}  [{c.get('fact_id') or 'no fact'}]")
    for i in result["check"]["unknown_ids"]:
        print(f"  FLAG cited fact ID {i} doesn't exist")
    if not result["check"]["passed"]:
        print("\nWarning: the checker flagged claims. Approving puts them in front of readers.")


def approval_gate(result, ask=input):
    """Return True only if a human reviewer types "approve". Anything else, or no answer, rejects."""
    show_for_review(result)
    try:
        answer = ask('\nType "approve" to save this draft. Anything else rejects it: ')
    except EOFError:
        answer = ""
    return answer.strip().lower() == "approve"


def save(result, approved, out_dir, stamp):
    folder = pathlib.Path(out_dir) / ("approved" if approved else "rejected")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"draft-{stamp}.md"
    path.write_text(result["draft"] + "\n", encoding="utf-8")
    return path


# Dry run: scripted replies so you can see the flow with no API key

def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(i, name, data):
    return SimpleNamespace(type="tool_use", id=i, name=name, input=data)


def _reply(blocks, stop="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(input_tokens=0, output_tokens=0))


class ScriptedClient:
    """Stands in for the Anthropic client. It returns the replies in `writer` and `checker` in order."""

    def __init__(self, writer, checker):
        self.writer = list(writer)
        self.checker = list(checker)
        self.messages = self
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        queue = self.checker if "tool_choice" in request else self.writer
        return queue.pop(0)


def dry_run_client():
    first = (
        "Lumen Notes 2.0 arrives March 3 with offline sync, so notes you edit without a connection "
        "sync when your device reconnects. It runs on Windows, macOS, iOS and Android. "
        "Existing users get the update free."
    )
    second = (
        "Lumen Notes 2.0 arrives March 3 with offline sync, so notes you edit without a connection "
        "sync when your device reconnects. It runs on Windows, macOS and iOS. "
        "Existing users get the update free."
    )
    writer = [
        _reply([_text("I'll look up the release facts."), _tool("toolu_01", "lookup_facts", {"query": "Lumen Notes 2.0 release"})]),
        _reply([_tool("toolu_02", "submit_draft", {"draft": first, "fact_ids": ["F01", "F02", "F03", "F06"]})]),
        _reply([_text("I'll remove Android."), _tool("toolu_03", "submit_draft", {"draft": second, "fact_ids": ["F01", "F02", "F03", "F06"]})]),
    ]
    supported = [
        {"claim": "Lumen Notes 2.0 arrives March 3", "status": "supported", "fact_id": "F01"},
        {"claim": "2.0 adds offline sync that syncs on reconnect", "status": "supported", "fact_id": "F02"},
        {"claim": "Existing users get the update free", "status": "supported", "fact_id": "F06"},
    ]
    checker = [
        _reply([_tool("toolu_c1", "report_claims", {"claims": supported + [
            {"claim": "It runs on Windows, macOS, iOS and Android", "status": "unsupported", "fact_id": ""}]})]),
        _reply([_tool("toolu_c2", "report_claims", {"claims": supported + [
            {"claim": "It runs on Windows, macOS and iOS", "status": "supported", "fact_id": "F03"}]})]),
    ]
    return ScriptedClient(writer, checker)


def main(argv=None, ask=input, client=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--task", default=DEFAULT_TASK, help="what the writer model should draft")
    p.add_argument("--facts", default="facts.json", help="the approved facts file")
    p.add_argument("--writer-model", default=WRITER_MODEL)
    p.add_argument("--checker-model", default=CHECKER_MODEL)
    p.add_argument("--max-steps", type=int, default=8, help="most model calls allowed in one run")
    p.add_argument("--max-revisions", type=int, default=1, help="how many times the writer may fix a failed draft")
    p.add_argument("--out", default="out", help="folder for approved and rejected drafts")
    p.add_argument("--logs", default="logs", help="folder for run logs")
    p.add_argument("--dry-run", action="store_true", help="use scripted replies. No API key, no cost")
    args = p.parse_args(argv)

    if client is None:
        if args.dry_run:
            client = dry_run_client()
        else:
            import anthropic
            client = anthropic.Anthropic()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = pathlib.Path(args.logs) / f"run-{stamp}.jsonl"
    run = Run(client, args.max_steps, log_path)
    facts = load_facts(args.facts)
    if args.dry_run:
        print("Dry run: a script supplies the model replies. No API calls, no cost.\n")
    run.log("start", task=args.task, writer_model=args.writer_model, checker_model=args.checker_model,
            max_steps=args.max_steps, dry_run=args.dry_run)

    try:
        result = run_agent(run, facts, args.task, args.writer_model, args.checker_model, args.max_revisions)
    except StepLimitReached:
        print(f"Stopped after {run.steps} model calls (the step limit). Nothing was saved.")
        print(f"Log: {log_path}")
        return 2

    approved = approval_gate(result, ask=ask)
    path = save(result, approved, args.out, stamp)
    run.log("decision", approved=approved, saved_to=str(path), model_calls=run.steps)
    print(f"\n{'Approved' if approved else 'Rejected'}. Saved to {path}")
    print(f"Model calls: {run.steps} of {args.max_steps}. Log: {log_path}")
    return 0 if approved else 1


if __name__ == "__main__":
    sys.exit(main())
