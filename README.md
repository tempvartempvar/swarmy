# swARMY

A testbench for comparing **chains of command for swarms of LLM agents**. Describe an order of
battle (ranks, how many subordinates each officer commands, a model per position), give it a task,
and swARMY runs it under a hard dollar budget, grades the result, and shows where the money and the
work went.

- **A visual order of battle.** `swarmy ui` opens an interactive org chart. Swap any position's
  model, add or remove ranks, define wikis and contact rules, then watch a run move through the
  army live.
- **Any model, any endpoint.** Anthropic, OpenRouter, OpenAI and Ollama are built in. Any
  OpenAI-compatible or Anthropic-compatible server can be added.
- **Structure as the variable.** Generate armies with the same number of soldiers at different
  depths, compare them with single-agent baselines, and vary information flow (wikis, direct
  contact) with everything else held fixed.
- **Tasks with exact answers**, generated from a seed: coding, research, both, and a greenhouse
  exercise that needs planning, research, simulation and testing. Or give the top position an
  open-ended mission.
- **Full trace.** Every order, report, model call and wiki edit is recorded, and the run explorer
  shows how each commander split up its part.

Positions default to military ranks: a *Field Marshal* on top, then generals, colonels and
captains, and *privates* doing the hands-on work. Titles are only labels; behaviour comes from a
position's place in the tree, its model, its tools and its goal.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env        # add ANTHROPIC_API_KEY and/or OPENROUTER_API_KEY
```

```bash
swarmy ui                   # http://127.0.0.1:8765
```

Check the wiring offline first. Mock runs use scripted agents that make no API calls; everything
they write is a labelled `[mock]` placeholder.

```bash
swarmy run --org configs/orgs/field-army.yaml --task mixed --mock
```

Then a real smoke test, which costs a few cents:

```bash
swarmy sweep configs/sweeps/smoke.yaml     # asks before spending
```

After `git pull`, restart `swarmy ui`; the page shows a banner if its code is out of date.

## The greenhouse exercise

An end-to-end exercise for a squad of cheap OpenRouter models (only `OPENROUTER_API_KEY` needed).
GreenLoop is piloting an irrigation controller for greenhouse Zone 3. The squad must:

1. **Research** the crop, pump, soil physics, rain, quiet hours, water budget and acceptance
   criteria in a 16-document knowledge base. Some documents are traps: another zone, a superseded
   note, next year's pump.
2. **Simulate**: build `sim.py`, one hour of soil moisture, from the documented physics.
3. **Build and test** `controller.py` against two official 30-day weather scenarios with
   `run_tests`, and **iterate** until every criterion passes.

Grading is automatic: hidden simulator checks (35%) and six hidden 30-day scenarios (65%). Each seed
is a different greenhouse, and naive controllers score zero.

`configs/orgs/openrouter-squad.yaml` is a Claude Haiku field marshal over research, engineering and
test captains (DeepSeek and GLM), with gpt-oss and GLM soldiers. One run costs a few cents to about
$0.30.

```bash
swarmy run --org configs/orgs/openrouter-squad.yaml --task greenhouse --budget 1
```

Or open `openrouter-squad` in the designer, pick **Greenhouse controller** in the Manoeuvres tab,
untick mock, and watch.

## The designer

- **Chart.** One band per rank. Each card shows a position's rank, name, model and effort. Drag to
  move, Ctrl+scroll to zoom, collapse any command. Wide armies split each rank into rows of units
  instead of shrinking.
- **Ranks** edits whole levels: how many subordinates each officer commands, insert or remove a
  rank, or build a balanced army from a few numbers.
- **Position** edits one position (or its whole level or rank): model, effort, goal, limits, name.
- **Wikis** and **Signals** set knowledge sharing, contact rules, order limits and context
  trimming, and draw who can read, write or contact whom on the chart.
- **Manoeuvres** runs the army, mock or real, under a budget. Cards light up as agents think, wait
  and answer, and show each position's latest five-word status. Live charts show cost and work
  (output tokens) over time for the top position, the officers and the soldiers, with a table of
  each group's share of cost, work, input tokens and calls.
- **Run explorer**: for any run, live or past, the chain of orders (every order, how it was split
  further, and the report that came back), the wikis with full history and diffs, the files, and
  the debug log.
- **Models** lists endpoints and models, tests a model with one real call, and adds your own
  (written to the git-ignored `configs/models.local.yaml`).

**Save** writes `configs/orgs/<name>.yaml`. The designer computes names, contacts and wiki
membership with the same code the engine runs, so the chart always matches what agents will do.

## Org configs

```yaml
name: regiment
levels:                    # top to bottom; every position in a level shares its settings
  - role: colonel          # positions are named <role>-<path>: captain-1, private-1.2, ...
    model: sonnet          # an alias from configs/models.yaml, or <endpoint>:<model-id>
    effort: low            # low | medium | high | xhigh | max
    goal: Keep the regiment's work tested and documented.
    span: 2                # direct subordinates per position
  - role: captain
    model: haiku
    span: 3
  - role: private          # the bottom level does the hands-on work
    model: gpt-oss-120b
```

For uneven structures or per-position models, write a `tree` instead (see
`configs/orgs/general-staff.yaml`): each node has `role`, `model`, optional `name` and `children`.

Optional per position or level: `goal`, `effort`, `max_turns`, `max_tokens`, `prompt` (extra
instructions), `can_do_work` (hands-on tools for an officer) and `review_tools` (officers may read
files and run public tests; on by default). Org-wide: `manager_max_turns` and `worker_max_turns`.

```bash
swarmy org configs/orgs/field-army.yaml     # tree and flatness metrics
```

**Missions.** Give the top position any open-ended objective, with an empty workspace and no
grading, then read how it was carried out:

```bash
swarmy run --org configs/orgs/field-army.yaml --budget 3 --mission "Build a simulation of orbital dynamics for LEO, MEO, GEO and a Molniya orbit, with plots."
```

```bash
swarmy orders      # the chain of orders as an outline (--full for whole texts)
```

## How the army works

### Orders

Commanders give orders with `delegate`. Every order is a structured, well-constrained task:

| field (characters) | holds |
|---|---|
| `objective` (required, 500) | the single outcome to achieve |
| `context` (2000) | only the facts this subordinate needs, distilled, or a pointer to a wiki page |
| `deliverables` (required, 1200) | exactly what to hand back, and in what form |
| `constraints` (1200) | interfaces, limits, what not to do |
| `done_when` (required, 600) | the test that shows it's finished |

- **Orders run in the background.** `delegate` returns at once, so a commander can start several
  units and keep working. Reports arrive as subordinates finish, at its next turn or through
  `wait`, which also wakes for a direct message. A subordinate takes one order at a time. A
  commander can't submit while orders are out; if it stops anyway, late reports are passed up with
  its own.
- **Scoped orders.** Orders to hands-on workers are shorter than orders to commanders. Commanders
  are told to give outcomes, not procedures.
- **No micromanagement.** An order to a commander that names anyone under it is rejected: that
  commander decides who does what.
- **No relaying.** Orders (and wiki edits) that repeat the commander's own orders verbatim are
  rejected.
- **Reports** are limited in length and end with a status of at most five words, shown on the chart.

Limits allow 10% over before rejecting, since a rejection costs a whole call.

```yaml
orders:
  max_chars: 3000          # orders to commanders
  max_chars_worker: 1800   # orders to hands-on workers
  max_report_chars: 3000
  no_micromanaging: true
  no_forwarding: true
```

### Wikis

```yaml
knowledge:
  wikis:
    - {name: orders, scope: org, write: managers, publish_brief: true}   # one wiki for everyone
    - {name: unit-log, scope: team}                   # one per officer and its direct subordinates
    - {name: division-file, scope: department, level: 1}   # one per level-1 position and everyone under it
```

`read` and `write` are `all`, `managers` or `workers`; commanders above a wiki's scope can always
read it. Agents are told to keep a curated knowledge base, not a log: one short, sourced page per
topic, read before writing, replaced with an improved version, with an edit summary.

Each run's wikis are a **git repository** (`results/runs/<run_id>/wiki/`): a folder per wiki, a
Markdown file per page, a commit per edit, authored by the agent and its rank. Browse them in the
run explorer, with `swarmy wiki`, or with any git tool:

```bash
git -C results/runs/<run_id>/wiki log --format='%h %an: %s' -p
```

### Direct contact

Orders flow down and reports flow up. These rules add direct contact; all are off by default.

```yaml
communication:
  chain: true          # commander <-> direct subordinate, mid-order
  peers: true          # same commander
  cross_team: false    # same rank, different commander
  open: false          # anyone with anyone
  skip_up: 1           # up to N ranks above one's own commander
  skip_down: 0         # officers: up to N ranks below their direct subordinates
  cc_skipped: true     # skipped officers get a copy
  modes: [ask, message]
  max_messages: 8      # per agent per run
  max_message_chars: 1500
  max_question_chars: 800
  max_answer_tokens: 600
```

`ask` gets an answer from a side conversation forked from the colleague's own history, so it
reflects what they know without interrupting them. `send_message` leaves a note they see next time
they act.

### Check-ins

When the top position stops before the job is done (out of turns, stopped calling tools, an error,
or submitted while the public tests still fail), the client checks in: it gives the reason and the
public test output, and asks it to finish or keep going. It keeps its conversation and gets fresh
turns.

```yaml
checkins:
  max: 2                  # 0 turns them off
  verify_tests: true      # also check in when public tests fail after a submit
  min_budget_frac: 0.15   # only with at least 15% of the budget and time left
```

### Context

When an agent's context passes `max_tokens`, older tool output, messages, reports and the content
of old tool calls (files it wrote) are cut to a short stub. Its orders and last `keep_turns` turns
stay intact. Trimming happens in one go, to about 60% of the limit, so prompt caching keeps working
between trims.

```yaml
context: {max_tokens: 30000, keep_turns: 4, trim_to: 400}
```

### Robustness

- **Turn budgets.** Agents know their budget and are warned with 3 turns left. Turns spent waiting
  for reports don't count. On the last turn a submit skips the soft checks (status length, report
  length, wiki reminder, orders still out); only a missing required field rejects it.
- **Preflight.** Before a real run, each model gets one tiny tool-enabled call. A bad key, no
  credits, an unknown model or a privacy setting that excludes every provider stops the run before
  it starts, with the fix. (`--no-preflight` skips it.)
- **Contained failures.** Only an endpoint refusal or an empty budget ends a run early. A tool
  error goes back to the agent; a broken agent reports an error to its commander.
- **Model quirks.** A submit written as text instead of a tool call is recovered; after a
  plain-text reply, OpenAI-compatible endpoints are asked for a tool call; a reply that hits the
  output limit gets a specific reminder.

## Runs and logs

```
results/
  swarmy.db                  # sweeps, runs, calls, events (SQLite)
  runs/<run_id>/run.log      # debug log: every call, error, check-in, and why the run ended
  runs/<run_id>/transcript.jsonl   # every input, step and tool result
  runs/<run_id>/workspace/   # the files the run produced
  runs/<run_id>/wiki/        # the wikis, as a git repository
  runs/<run_id>/result.json  # grade, cost per position, end reason
  reports/<sweep>-<id>/      # report.md, charts, runs.csv, levels.csv
```

```bash
swarmy log [RUN]           # the debug log (--all for every call); `swarmy run -v` streams it live
```

```bash
swarmy orders [RUN]        # the chain of orders
```

```bash
swarmy wiki [RUN]          # wiki pages and who edited them
```

## Models and endpoints

| endpoint | serves | key in `.env` |
|---|---|---|
| `anthropic` | Claude (streaming, adaptive thinking, effort, prompt caching) | `ANTHROPIC_API_KEY` |
| `openrouter` | hundreds of models; reports the real cost of each call | `OPENROUTER_API_KEY` |
| `openai` | OpenAI | `OPENAI_API_KEY` |
| `ollama` | local models at `localhost:11434` | none |
| `mock` | scripted offline agents | none |

Add endpoints and models in `configs/models.yaml` or the Models dialog:

```yaml
endpoints:
  vllm:
    type: openai                  # openai (chat completions) or anthropic (Messages API)
    base_url: http://gpu-box:8000/v1
    api_key_env: VLLM_API_KEY     # omit if none
models:
  big-local:
    endpoint: vllm
    model: Qwen/Qwen3-32B
    price: {input: 0, output: 0}  # USD per million tokens; needed for the budget guard
```

Any position can also name a model inline, as `<endpoint>:<model-id>`
(`openrouter:z-ai/glm-5.3-flash`, `ollama:qwen3:8b`). Hands-on models must support tool calling.

```bash
swarmy models               # endpoints (with key status) and models
```

```bash
swarmy models check         # compare catalog prices and tool support with OpenRouter
```

```bash
swarmy models test glm-flash   # one short real call
```

**OpenRouter privacy settings.** If your account requires Zero Data Retention, only models with a
ZDR provider work ([list](https://openrouter.ai/api/v1/endpoints/zdr)). Every model in
`openrouter-squad` has one.

To swap models without editing files, use `--model ROLE=MODEL` (or `top` / `bottom`),
`--effort ROLE=EFFORT` and `--set path=value` with `swarmy run` and `swarmy org`:

```bash
swarmy run --org configs/orgs/regiment.yaml --task code --model private=glm-flash --set levels.1.span=4
```

## Sweeps

A sweep runs several orgs on several tasks under one budget.

```yaml
name: pilot
budget_usd: 5.0            # hard ceiling for the whole sweep
concurrency: 3
orgs:
  - path: configs/orgs/regiment.yaml
    matrix: {private: [gpt-oss-120b, glm-flash], effort.colonel: [low, medium]}
  - generate:              # same soldiers, different depths
      name: army
      workers: 8
      depths: [1, 2, 3]
      include_solo: true   # plus a single-agent baseline
      ladder: [{model: sonnet}, {model: haiku}]   # officer tiers, top down
      worker: {model: gpt-oss-120b}
tasks:
  - {family: code, params: {n: 6, coupling: 0.3}, seeds: [1, 2]}
```

```bash
swarmy plan configs/sweeps/pilot.yaml      # what will run and the per-run caps
```

```bash
swarmy sweep configs/sweeps/pilot.yaml
```

```bash
swarmy report                              # the latest sweep's report
```

**Budgets are hard.** Before every call, `max_tokens` is clamped to what the run can still afford
in the worst case. Each run reserves its cap before starting, so concurrent runs can't overshoot the
sweep. Runs are interleaved so every org has results on the same tasks if the budget runs out.

**The report** compares orgs on score, cost per run and per pass, time, cost by level, the
coordination share (spend going to officers), coverage and redundancy of work handed to soldiers,
rework, review gain, parallelism, contact and wiki use, and failure modes, with 95% confidence
intervals, a quality-versus-cost Pareto chart and cost-by-level charts.

| included sweep | question |
|---|---|
| `smoke` | do keys, providers and grading work? |
| `structure-control` | does shape matter when every agent runs the same model? |
| `pilot` | flatness with a realistic model mix |
| `model-swap` | privates' model × colonel's effort |
| `chain-of-command` | a five-rank army, flattened step by step |
| `communication` | same structure and models, different information flow |
| `greenhouse` | lone Haiku vs Haiku with soldiers vs the squad, on the greenhouse |

**Designing experiments.** Change one thing at a time: use `structure-control` (one model
everywhere) to separate structure from model mix, and `communication` to vary only information
flow. Use several seeds and read the confidence intervals. Hierarchy should help on parallel tasks
(`qa:kind=aggregate`, `code:coupling=0`) and hurt on sequential ones (`qa:kind=chain`,
`code:coupling=1`). Prompts come from one template, so only facts about the org change between
structures.

## Tasks

| spec | tests |
|---|---|
| `qa:kind=chain,hops=3` | a sequential multi-hop lookup |
| `qa:kind=aggregate,n=6` | independent lookups, then an argmax (fully parallel) |
| `qa:kind=census` | scan, filter and count many documents |
| `code:n=6,coupling=0.3` | implement precisely specified functions against hidden tests; `coupling` sets how many build on others |
| `mixed:products=4,cities=3` | find pricing rules in documents, then implement `quote(order)` |
| `greenhouse` | the greenhouse exercise above |
| `--mission "..."` | your own open-ended objective, ungraded |

Tasks are generated from the seed (a fictional world of about 110 documents with distractors, or
randomized code specs), so nothing can be memorized, and grading is exact.

```bash
swarmy task qa:kind=chain,hops=4 --seed 3 --gold     # read a brief and its answer
```

## Caveats

- **Model-written code is not securely sandboxed.** It runs in a subprocess without API keys, with
  CPU, memory and time limits, but that is not a security boundary. Run real sweeps in a container.
- **Prices.** OpenRouter calls log the cost OpenRouter reports. Other calls are costed from the
  catalog prices (Anthropic's are list prices), which the budget guard also uses.
- **Refusal fallback.** Opus and Sonnet retry a safety refusal on a fallback model. Those calls are
  flagged in the data; set `refusal_fallback: false` in `configs/models.yaml` to turn it off.

## Extending

- **A task family:** subclass `TaskInstance` (`swarmy/tasks/base.py`), set `brief` and `grade()`,
  optionally a corpus, workspace files and public tests, and register it in `swarmy/tasks/__init__.py`.
- **A provider:** implement `new_conversation()` and `step()` (`swarmy/providers/base.py`) and add
  it to `ProviderRegistry`.
