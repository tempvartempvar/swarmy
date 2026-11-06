# orgbench

A testbench for comparing **organizational hierarchies of LLM agents**. You describe an org
chart (levels, span of control, a model and effort level per tier), point it at a suite of
coding and research tasks, and orgbench runs it under a hard dollar budget, grades the
output, and reports quality, cost, and coordination metrics for each structure.

```
ceo (opus, max) ─┬─ c-suite-1 (opus, medium) ─┬─ director-1.1 (sonnet) ─┬─ manager-1.1.1 (haiku) ─┬─ worker-1.1.1.1 (free model)
                 │                             │                         │                         └─ worker-1.1.1.2
                 ...                           ...                       ...
```

- **A visual org designer.** `orgbench ui` opens an interactive org chart in your browser. You can
  swap any position's model, add or remove positions and management layers, define wikis and
  communication rules, and watch a test run move through the org live.
- **Any model at any position.** Claude goes through the Anthropic API; everything else goes through
  OpenRouter. Swap models in the designer, from the CLI, in YAML, or as a sweep matrix.
- **Knowledge sharing and communication as variables.** Wikis can be scoped to the whole org, each
  department or each team, with read and write permissions. Direct contact (manager to report,
  between teammates, across teams, skip-level, or fully open) can be switched on with rules such as
  copying the skipped managers.
- **Flatness is a parameter.** You can generate orgs with the same number of workers and different
  depths, compare against single-agent baselines, and use a same-model control to separate the
  effect of structure from the effect of the model mix.
- **Tasks with exact answers.** Coding, research/QA and mixed research-plus-coding tasks are
  generated from a seed. Nothing can be memorized, every task has an exact answer, and you can
  dial how parallelizable each one is.
- **Budget guard.** Every call is checked before it is sent, so a run cannot exceed its cap and a
  sweep cannot exceed its budget (default $5).
- **Full trace.** Every call, delegation, report and tool use goes into SQLite and a per-run
  JSONL transcript. Each run's final workspace is kept.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env        # add ANTHROPIC_API_KEY and OPENROUTER_API_KEY
```

Open the designer:

```bash
orgbench ui                  # http://127.0.0.1:8765
```

Check everything offline first. The scripted mock provider runs the whole pipeline without
making any API calls:

```bash
orgbench run --org configs/orgs/corporate-5tier.yaml --task mixed --mock
orgbench sweep configs/sweeps/pilot.yaml --mock
pytest
```

Then do a real smoke test, which costs a few cents:

```bash
orgbench plan  configs/sweeps/smoke.yaml     # what will run, and the per-run caps
orgbench sweep configs/sweeps/smoke.yaml     # asks for confirmation before spending
orgbench report                              # latest sweep -> results/reports/<name>-<id>/report.md
```

## Org designer

```bash
orgbench ui [--port 8765] [--orgs-dir configs/orgs]
```

- **Chart.** Each level is a horizontal band. A card shows the position's title, name, model
  (coloured by family) and effort, plus counts of the wikis (▤) and contacts (⇄) it has. Drag to pan,
  scroll to zoom, collapse subtrees, and use **Stack teams** to put each team's workers in a column
  so wide orgs stay readable.
- **Position tab.** Click a card to see who the position reports to, its direct reports, its rough
  cost per call, its wiki access and who it can contact. You can change its model, effort, limits,
  title, name and role notes. The scope switch applies an edit to this position only, the whole
  level, or every position with the same title. Structure buttons add a direct report, duplicate the
  position (with its team), insert a manager above it, or remove it.
- **Structure tab.** Change how many reports each manager at a level has (− / +), insert a
  management layer below a level (each manager's reports are split among new managers), remove a
  level, edit a whole level at once, or build a balanced org from a few numbers.
- **Knowledge tab.** Add wikis and set their scope, who can read and write them, oversight, and
  whether the client's brief is posted on them. Selecting a wiki outlines each instance on the chart
  and marks who can write (solid border) and who can only read (dashed border).
- **Communication tab.** Toggle the contact rules and set the skip-level distance. Selecting a
  position draws a line to every colleague it may contact, coloured by type.
- **Test run tab.** Run the org on a task, in mock mode or with real models under a hard budget, and
  watch it live. Agents light up while they think, wait or answer; assignments and reports flash
  along the edges; messages arc between cards; and an activity feed shows assignments, reports,
  questions with their answers, and wiki edits as they happen.
- **Save** writes `configs/orgs/<name>.yaml`, using the compact `levels` form when the tree is
  uniform and an explicit `tree` otherwise. **YAML** shows the config and lets you paste one in.
  Undo and redo cover every edit.

The designer gets everything it shows (names, metrics, contacts, wiki membership) from the same
code the engine runs, so the chart always matches what the agents will do.

## Org configs

```yaml
# configs/orgs/two-tier.yaml
name: two-tier
levels:                     # top to bottom; every node in a level shares its settings
  - role: lead
    model: sonnet           # alias from configs/models.yaml, or openrouter:<id> / anthropic:<id>
    effort: low             # low | medium | high | xhigh | max  (maps to Claude's effort / OpenRouter reasoning)
    span: 2                 # direct reports per node
  - role: manager
    model: haiku
    span: 3
  - role: worker            # bottom level: span 0, does the hands-on work
    model: gpt-oss-120b
memory: persistent          # persistent: an agent remembers earlier assignments; fresh: starts clean each time
```

For uneven structures or per-position models, write an explicit `tree` instead of `levels`
(see `configs/orgs/research-lab.yaml`):

```yaml
tree:
  role: director
  model: sonnet
  children:
    - role: lead
      name: research-lead          # optional custom name (default: <role>-<path>, e.g. lead-1)
      model: haiku
      children: [{role: researcher, model: gpt-oss-120b}, {role: researcher, model: qwen-flash}]
    - {role: engineer, model: haiku}
```

### Knowledge sharing

```yaml
knowledge:
  wikis:
    - name: handbook
      scope: org              # org | department | team
      write: managers         # who can write: all | managers | workers
      read: all               # who can read
      publish_brief: true     # post the client's original assignment on it
      description: Plans and decisions from management.
    - name: team-notes
      scope: team             # one wiki per team: a manager plus their direct reports
      oversight: true         # managers above the team can read it (default)
    - name: division
      scope: department
      level: 1                # one wiki per level-1 position, shared by everyone under it
```

Agents get `wiki_read`, `wiki_search` and (where permitted) `wiki_write` for the wikis they can
reach. Their prompt lists each wiki and its scope. Final wiki contents are saved with each run.

### Communication rules

Assignments always flow down the tree (`delegate`) and reports flow up (`submit`). These rules add
direct contact, and all of them are off by default:

```yaml
communication:
  chain: true          # manager <-> direct report, mid-assignment
  peers: true          # teammates (same manager)
  cross_team: false    # same level, different manager
  open: false          # anyone may contact anyone
  skip_up: 1           # may contact up to 1 level above their own manager
  skip_down: 0         # managers may contact up to N levels below their direct reports
  cc_skipped: true     # skipped managers get a copy of skip-level traffic
  modes: [ask, message]
  max_messages: 8      # per agent per run
```

- `ask` asks a colleague a question and waits for the answer. The colleague answers in a side
  conversation forked from their own history, so the answer reflects what they actually know; their
  main work isn't interrupted, and they get an FYI note about the exchange.
- `send_message` leaves a note that the recipient sees the next time they act. Notes that are never
  read are counted as `undelivered`.

Each agent's tools list exactly the colleagues it may contact, and its prompt says how each one
relates to it (manager, teammate, skip-level and so on).

Optional level fields: `max_tokens`, `max_turns`, `can_do_work` (gives managers hands-on
tools as well), `review_tools` (lets managers read the workspace and run the public tests;
on by default), and `prompt` (extra role instructions).

To see an org's tree and flatness metrics:

```bash
orgbench org configs/orgs/corporate-5tier.yaml
```

### Swapping models

```bash
# per role (or 'top' / 'bottom'), on any command
orgbench run --org configs/orgs/corporate-5tier.yaml --task code \
    --model worker=openrouter:qwen/qwen3.7-flash --model ceo=sonnet --effort director=high
# any field, by dotted path
orgbench run --org configs/orgs/two-tier.yaml --task qa:kind=aggregate --set levels.1.span=4 --set communication.peers=true
```

In a sweep, `models:`, `efforts:` and `set:` apply fixed overrides, and `matrix:` expands into
a Cartesian product (see `configs/sweeps/model-swap.yaml`):

```yaml
orgs:
  - path: configs/orgs/two-tier.yaml
    matrix:
      worker: [gpt-oss-120b, qwen-flash, haiku]
      effort.lead: [low, medium]
```

The model catalog is `configs/models.yaml`. Writing `openrouter:<model-id>` uses any
tool-capable OpenRouter model with live pricing, no catalog edit needed. `orgbench models check`
compares the catalog with OpenRouter's current prices and tool support.

### Generating structures that vary only in depth

```yaml
- generate:
    name: corp
    workers: 8              # held constant
    depths: [1, 2, 3, 4]    # management layers above the workers
    include_solo: true      # also add a single-agent baseline
    ladder:                 # management tiers, top-down
      - {role: ceo, model: opus, effort: max}
      - {role: c-suite, model: opus, effort: medium}
      - {role: director, model: sonnet}
      - {role: manager, model: haiku}
    worker: {model: gpt-oss-120b}
```

Spans are split as evenly as possible, with larger spans at the bottom: 8 workers at depth 3
gives 1-2-4-8. If the worker count runs out of factors, the upper levels get span 1 (a pure
relay layer, which tests the "telephone game" effect). When a structure is flattened, tiers
are removed from the middle and the top tier is always kept. A one-entry `ladder` gives every
manager the same model.

## Tasks

| family | spec | what it tests | parallelizable? |
|---|---|---|---|
| `qa` | `qa:kind=chain,hops=2..4` | sequential multi-hop lookup | barely: each hop depends on the last |
| `qa` | `qa:kind=aggregate,n=6` | n independent 2-hop lookups, then an argmax | fully |
| `qa` | `qa:kind=census` | scan many docs, filter, count | by partitioning the docs |
| `code` | `code:n=6,coupling=0.3` | implement n precisely specified functions (one module each) against hidden tests | `coupling` sets how many functions build on others: 0 = independent, 1 = chains |
| `mixed` | `mixed:products=5,cities=4,rules=5` | find pricing, shipping and tax policy in the knowledge base, then implement `quote(order)` | research fans out; the code needs everything put together |

All tasks are generated from the seed in the spec. The world is a fictional set of countries,
cities, companies, people and products, written up as about 110 short documents. Facts are
spread across documents (for example, a company's CEO appears only on that person's page) and
there are distractor news items. Code specs use randomized parameters, so the specs differ from
seed to seed. Grading is exact: QA answers are normalized and matched, and code is scored by the
fraction of hidden tests passed, in a subprocess.

To read a task brief and its gold answer:

```bash
orgbench task qa:kind=chain,hops=4 --seed 3 --gold
```

## Sweeps and budgets

```yaml
name: pilot
budget_usd: 5.0          # hard ceiling for the whole sweep
run_budget_usd: null     # default: budget / number of runs (an org entry can override)
concurrency: 3
repeats: 1
orgs: [...]
tasks:
  - {family: code, params: {n: 6, coupling: 0.3}, seeds: [1, 2]}
```

How the guarantees work:

- **Per call.** Before each request, `max_tokens` is clamped to what the run can still afford,
  assuming the worst case (the whole prompt billed as a cache write and every output token
  used). When the run can't afford 1,024 output tokens, it stops with status
  `budget_exhausted`, and whatever it produced is still graded.
- **Per sweep.** Each run reserves its cap before it starts and returns the unused part when it
  finishes, so concurrent runs can't overshoot the budget. Runs that don't fit are recorded as
  `skipped_budget`.
- **Run order.** Runs are interleaved (seed, then task, then org). If the budget runs out
  part-way, every org still has results on the same tasks.
- `orgbench plan` warns when a level's single call could cost more than half the run cap. For
  example, an Opus max-effort call with 32k output costs about $0.66.
- Real sweeps ask for confirmation (`--yes` skips the prompt). If a fatal provider error happens
  (bad key, unknown model, free-tier daily quota used up), the sweep stops.

### Rough per-call costs

These assume about 8k prompt tokens (mostly cached) and 1.5k output tokens:

| model | ≈ $/call | notes |
|---|---|---|
| Opus 5.5 | 0.03-0.05 | max effort can produce 10k+ thinking tokens, which is $0.20+ per call |
| Sonnet 5.5 | 0.015-0.025 | |
| Haiku 5.5 | ~0.001 | cheap enough for multi-seed sweeps |
| gpt-oss-120b / qwen-flash (OpenRouter) | ~0.0003 | good default workers |
| `:free` models | 0 | rate-limited (~20 req/min plus a daily cap), so they stall sweeps. Free endpoints may log prompts. |

### Included sweeps (all ≤ $5)

| sweep | question | runs |
|---|---|---|
| `smoke` | do keys, providers and grading work? | 4 (≤ $0.50) |
| `structure-control` | **does shape matter when every agent runs the same model (Haiku)?** depth 0-3 plus a team-board variant, 3 tasks × 3 seeds | 45 |
| `pilot` | flatness with a realistic model mix (Sonnet lead, Haiku managers, cheap workers) at 4 workers | 16 |
| `model-swap` | same structure; worker model × lead effort | 12 |
| `corporate-ladder` | the full Opus→Opus→Sonnet→Haiku→worker ladder, flattened step by step at 8 workers | 4 |
| `communication` | **same structure and model, different information flow**: strict tree vs team wiki vs org wiki vs peers vs skip-level vs open | 36 |

## Metrics (in `report.md`, `runs.csv`, `levels.csv`)

- **score / pass**: grade in [0, 1]; pass means a score of 1.
- **$/run, $/pass, wall time, calls, tokens** (cached, uncached and reasoning tokens separately).
- **cost by level**: where the money goes in each org.
- **coordination share**: share of spend and tokens going to managers rather than workers.
- **delegations / rework**: how many assignments were made, and how many were repeat assignments
  to the same agent.
- **coverage**: fraction of the task's key items (functions, products, cities...) that appear in at
  least one first-round worker assignment. If this is low, work got lost on the way down the
  hierarchy.
- **redundancy**: average number of workers given the same item. 1.0 means no duplicated work.
- **review gain** (code/mixed): final score minus the score when the top agent's first round of
  delegation came back. This measures whether review at the top caught errors.
- **parallelism**: total model time divided by wall time.
- **messages / asks / skip-level**: direct contacts outside delegate/report, and how many of them
  bypassed a manager. **answer $** is the share of spend on answering colleagues' questions.
- **wiki writes / reads**: knowledge-base use. **undelivered**: inbox notes nobody read.
- **rate_wait_s**: time spent queued or backing off on rate limits, recorded separately from model
  latency.
- **nudges, turn limits, tool errors, agent errors, fallback calls**: failure modes, mostly of the
  small models.

The report also has a quality-vs-cost Pareto plot, a stacked cost-by-level chart, and scores
broken down by task family.

## Experiment design notes

- **Separate structure from model mix.** In the corporate ladder, a deeper org also has more
  expensive managers, so depth and model quality change together. Run `structure-control`, which
  uses the same model everywhere, alongside any mixed-model sweep.
- **Vary task parallelizability.** Hierarchy should help on `qa:kind=aggregate` and
  `code:coupling=0` and hurt on `qa:kind=chain` and `code:coupling=1`. The interesting result is
  where it flips.
- **Repeat runs.** LLM runs vary. Use several seeds, `repeats`, or both, and read the 95% CIs in
  the report. Differences inside the CI are noise.
- **Use a fixed baseline.** Include `solo-*` orgs, a single strong agent and a single cheap worker,
  so the structures have something to beat.
- **Isolate information flow.** The `communication` sweep keeps structure and models fixed and
  varies only wikis and contact rules.
- **Keep prompts constant.** Role prompts come from one template, and only facts about the org
  (shape, the agent's position, its wikis and contacts) change between structures. If you add `prompt:` to a level, apply the same text to every
  structure you compare.

## Results layout

```
results/
  orgbench.db                       # sweeps, runs, calls, events (SQLite)
  runs/<run_id>/transcript.jsonl    # every input, step (text, thinking summary, tool calls) and tool result
  runs/<run_id>/workspace/          # final shared workspace
  runs/<run_id>/wikis/              # final wiki pages, one folder per wiki instance
  runs/<run_id>/result.json         # grade and per-node cost and calls
  reports/<sweep>-<id>/             # report.md, charts, runs.csv, levels.csv, summary.json
```

## Notes and caveats

- **Claude API settings.** Claude calls use streaming, adaptive thinking, `output_config.effort`
  and automatic prompt caching. Conversations are append-only, so the cache and replayed thinking
  stay valid. Thinking summaries are saved to transcripts.
- **Refusal fallback is on for Opus and Sonnet.** These models send `fallbacks: "default"`, so a
  safety refusal is retried on a fallback model instead of ending the turn. Fallback calls are
  flagged (`calls.fallback`, and `fallback_calls` in `runs.csv`) so you can exclude them. Set
  `refusal_fallback: false` in `configs/models.yaml` to turn it off.
- **Model-written code is not securely sandboxed.** It runs in a subprocess with no API keys in
  its environment, plus CPU, memory and file-size limits and a timeout. That is not a security
  boundary. Run real sweeps in a container or VM.
- **Prices.** Anthropic prices are list prices. OpenRouter prices in the catalog are a snapshot
  (`orgbench models check` compares them with live prices). For OpenRouter calls, the cost the
  provider reports is what gets logged.

## Extending

- **New task family:** subclass `TaskInstance` (`orgbench/tasks/base.py`) and set `brief`,
  `grade()`, and optionally `corpus`, `workspace_files`, `public_tests()`, `progress()` and
  `key_terms`. Then register it in `orgbench/tasks/__init__.py`.
- **New provider:** implement `Provider.new_conversation()` and `Provider.step()`
  (`orgbench/providers/base.py`), then add it to `ProviderRegistry`.
