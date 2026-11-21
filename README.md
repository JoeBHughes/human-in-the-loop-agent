# Put guardrails on an AI agent before you let it publish

An agent that writes content can state things that aren't true. This reference build shows five guardrails that keep a human in charge of what goes out. A writer model drafts a short announcement from approved facts, and a second model checks every claim. The agent saves nothing as approved until a person says so.

Independent project by Joe Hughes. Anthropic has not reviewed or endorsed this project. Lumen Notes, the product in the sample facts, is fictional.

## What you'll do

| Step | Time | What happens |
|---|---|---|
| 1. Set up | 5 minutes | Download the kit. The dry run needs no packages |
| 2. Watch a dry run | 5 minutes | See every guardrail fire, with scripted model replies and no API key |
| 3. Read the log | 5 minutes | Trace each step the agent took |
| 4. Run it for real | 5 minutes | Point the agent at the Claude API |
| 5. Use your own facts | 10 minutes | Replace the sample facts and task with yours |

## How a run works

1. The writer model gets a task, such as "Write a three-sentence announcement of the Lumen Notes 2.0 release."
2. The writer looks up facts with the `lookup_facts` tool. That tool can only return facts from `facts.json`.
3. The writer submits a draft and lists the IDs of the facts it used.
4. A second model, the checker, splits the draft into claims and marks each one supported or unsupported. An automated script confirms that every cited fact ID exists.
5. If anything fails, the agent sends the flagged claims back to the writer for one revision.
6. A human reviewer sees the draft and the claim check, then approves or rejects it.

Every model call counts toward a step limit, and an automated logger records every step.

## 1. Set up

You need Python 3.9 or later. On Windows, type `py` wherever this guide says `python`.

```bash
git clone https://github.com/JoeBHughes/human-in-the-loop-agent.git
cd human-in-the-loop-agent
```

No Git? On the repository page, click **Code**, then **Download ZIP**. Unzip it and open a terminal in the `human-in-the-loop-agent` folder.

## 2. Watch a dry run

```bash
python agent.py --dry-run
```

In a dry run, a script supplies the model replies, so you can see the whole flow before you spend anything. The scripted writer makes a mistake on purpose. It says the app runs on Android, and no approved fact says that.

```text
Dry run: a script supplies the model replies. No API calls, no cost.

Step 1: writer looked up "Lumen Notes 2.0 release" and got 6 approved facts
Step 2: writer submitted a draft
Step 3: checker flagged 1 of 4 claims
        Sending the flagged claims back to the writer
Step 4: writer submitted a draft
Step 5: checker found 4 claims, all supported

=== Draft for review ===

Lumen Notes 2.0 arrives March 3 with offline sync, so notes you edit without a connection sync when your device reconnects. It runs on Windows, macOS and iOS. Existing users get the update free.

Facts cited: F01, F02, F03, F06
Revisions: 1

Claim check:
  OK   Lumen Notes 2.0 arrives March 3  [F01]
  OK   2.0 adds offline sync that syncs on reconnect  [F02]
  OK   Existing users get the update free  [F06]
  OK   It runs on Windows, macOS and iOS  [F03]

Type "approve" to save this draft. Anything else rejects it:
```

Type `approve` and press Enter. The draft goes to `out/approved/`. Type anything else, or press Enter with nothing, and it goes to `out/rejected/`.

To watch the step limit stop a run, allow only three model calls:

```bash
python agent.py --dry-run --max-steps 3
```

The run ends with:

```text
Stopped after 3 model calls (the step limit). Nothing was saved.
```

## 3. Read the log

Each run writes a log to `logs/`, one JSON entry per line. Open the newest file. You'll see these kinds of entries:

| Entry | What it records |
|---|---|
| `start` | The task and the settings for the run |
| `model_call` | The model's reply and the tokens it used |
| `tool_call` | Each fact lookup with its results, and each submitted draft |
| `claim_check` | Each claim with its status, plus any cited fact IDs that don't exist |
| `step_limit` | The point where the run stopped, if it hit the limit |
| `decision` | Whether the reviewer approved, and where the draft was saved |

When a draft goes wrong, the log answers "where did that claim come from?" You can see which facts the writer looked up and what the checker said about each claim.

## 4. Run it for real

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...        # PowerShell: $env:ANTHROPIC_API_KEY = Read-Host "Key"
python agent.py
```

A typical run makes 3 to 5 model calls. The writer defaults to `claude-sonnet-5-5` and the checker to `claude-haiku-5-5`. Change them with `--writer-model` and `--checker-model`, using model IDs that the Models API lists today.

Real models vary. Your run may pass the first claim check, or the checker may flag a claim the dry run didn't. Both outcomes show the guardrails working.

## 5. Use your own facts

Edit `facts.json`. Each fact needs an ID and one statement:

```json
{"id": "F07", "text": "Lumen Notes 2.0 supports dark mode."}
```

Keep each fact to one claim. The checker matches claims to facts one at a time, so a fact that bundles three claims is harder to check.

Then give the writer your own task:

```bash
python agent.py --task "Write a two-sentence release note about dark mode for the help center."
```

## How each guardrail works

### Approved facts only

The writer can't read anything except what `lookup_facts` returns, and that tool reads only `facts.json`. When a lookup finds nothing, the tool tells the writer not to state anything about that topic. The system prompt repeats the rule.

This guardrail limits what the writer sees. It can't stop the writer from inventing something, which is why the next guardrail exists.

### Claim check

The checker is a second model with one job. It gets the approved facts and the draft, and reports each claim with the `report_claims` tool. The agent forces that tool with `tool_choice`, so the checker always returns structured results.

The checker is a model, so it can be wrong. An automated script adds one check that doesn't depend on a model: every fact ID the writer cites must exist in `facts.json`.

### Step limit

Every model call goes through one function that counts calls. When a run reaches `--max-steps` (8 by default), it stops and saves nothing. A writer stuck in a loop can't run up your bill.

`--max-revisions` (1 by default) sets how many times the writer may fix a failed draft. After that, the draft goes to the reviewer with the flags still showing.

### Logging

The same function that counts calls also logs them. Tool calls and claim checks go in the same file, and the reviewer's decision goes in last. Because one function handles every call, no step can skip the log.

### Approval gate

The script saves a draft as approved only when a human reviewer types `approve`. Any other answer rejects it. So does no answer at all, which matters if someone runs the agent from a scheduled job.

There's no flag to skip the gate. If you add one, the agent can publish without a person reading it, and every guardrail before the gate becomes advisory.

## What these guardrails don't cover

- The checker can miss a claim or mark a false claim as supported. Have a human reviewer spot-check its results, the same way you'd check any grader.
- `lookup_facts` matches words, so it can miss a fact phrased differently from the query. A search tool that matches meaning would find more.
- The step limit counts model calls, not tokens. A single long call still costs more than a short one. Set `max_tokens` with that in mind.
- The approval gate trusts the reviewer. It shows the flags, but it can't make anyone read them.

## Where to go next

- Run the checker twice, or with two different models, and flag any claim they disagree on.
- Add a style check before the gate, such as a banned-word list or a length limit.
- Score many runs with the eval from [first-eval](https://github.com/JoeBHughes/first-eval) to see how often the first draft passes.

## Files

| File | What it does |
|---|---|
| `agent.py` | The agent, its guardrails and the dry-run script |
| `facts.json` | Six sample facts about a fictional app |
| `tests/` | One or more tests per guardrail. Run them with `pip install pytest`, then `python -m pytest tests` |

## License

MIT
