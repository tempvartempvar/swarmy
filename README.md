# swARMY

A testbench for comparing **chains of command for swarms of LLM agents**. You describe an order
of battle (ranks, how many subordinates each officer commands, a model and effort level per
position), point it at a suite of coding and research tasks, and swARMY runs it under a hard dollar
budget, grades the output, and reports quality, cost, and coordination metrics for each structure.

Positions default to military ranks: a *Field Marshal* at the top, then generals, colonels and
captains, with *privates* doing the hands-on work and a *lone wolf* as the single-agent baseline.
Titles are only labels: behaviour comes from a position's place in the tree, its model, its tools
and its goal. Rename any position to anything you like.

Give the Field Marshal a mission in plain words ("build a simulation of orbital dynamics across
different orbits") and watch how it splits the work according to its forces, and how each order
is passed down and turned into further orders until it reaches the privates.

```
field-marshal (opus, max) ─┬─ general-1 (opus) ─┬─ colonel-1.1 (sonnet) ─┬─ captain-1.1.1 (haiku) ─┬─ private-1.1.1.1 (any model)
                           │                    │                        │                        └─ private-1.1.1.2
                 ...                           ...                       ...
```

- **A visual order of battle.** `swarmy ui` opens an interactive org chart in your browser. You can
  swap any position's model, add or remove positions and ranks, define wikis and communication
  rules, watch a test run move through the org live, and read the wikis it wrote, with who changed
  what.
- **Any model, any endpoint.** Anthropic, OpenRouter, OpenAI and a local Ollama are built in, and any
  OpenAI-compatible server (vLLM, LM Studio, llama.cpp, Together, Groq, Gemini, a LiteLLM gateway...) or
  Anthropic-compatible gateway can be added. Swap models in the designer, from the CLI, in YAML, or as
  a sweep matrix.
- **Missions and goals.** Give the top position an open-ended mission and give any position or level a
  standing goal. The run explorer shows the chain of orders: every order, the report that came back for
  each, and how each commander split up its part.
- **Knowledge sharing and communication as variables.** Wikis can be scoped to the whole org, each
  division or each unit, with read and write permissions. Direct contact (commander to subordinate,
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
swarmy ui                  # http://127.0.0.1:8765
```

Check everything offline first. The scripted mock provider runs the whole pipeline without
making any API calls. Mock agents don't think: every order, note and report they write is a
labelled `[mock]` placeholder, and mock runs are marked MOCK throughout the designer.

```bash
swarmy run --org configs/orgs/field-army.yaml --task mixed --mock
swarmy sweep configs/sweeps/pilot.yaml --mock
pytest
```

Then do a real smoke test, which costs a few cents:

```bash
swarmy plan  configs/sweeps/smoke.yaml     # what will run, and the per-run caps
swarmy sweep configs/sweeps/smoke.yaml     # asks for confirmation before spending
swarmy report                              # latest sweep -> results/reports/<name>-<id>/report.md
```

## Worked example: the greenhouse exercise (OpenRouter only)

A small, end-to-end exercise that needs planning, research, simulation, product testing and
iteration, run by a squad of cheap OpenRouter models. You only need `OPENROUTER_API_KEY` in `.env`.

**The job.** GreenLoop is piloting an automatic irrigation controller for greenhouse Zone 3. The
team has to:

1. **Plan:** split the work between research, engineering and testing.
2. **Research:** find, in a 16-document knowledge base, which crop and pump Zone 3 actually has, the
   soil physics, how rain is absorbed, the quiet hours, the water budget and the acceptance criteria.
   Some documents are traps: another zone, a superseded 2019 physics note, a memo about next year's
   pump.
3. **Simulate:** build `sim.py`, one hour of the zone's soil moisture, from the documented physics,
   and use it to try controller strategies.
4. **Product-test:** `run_tests` checks `sim.py` on sample inputs and runs `controller.py` through two
   official 30-day weather scenarios, reporting every criterion: time in the crop's moisture band,
   never wilting, the water budget, no pumping in quiet hours.
5. **Iterate** until everything passes, and write up what changed in `TEST_REPORT.md`.

It's graded automatically: hidden simulator checks (35%) plus six hidden 30-day scenarios (heat
wave, rainy and dry spells...) that the controller must pass (65%). Each seed is a different
greenhouse, and naive controllers (never water, flood by day, ignore quiet hours) score zero.

**The squad** (`configs/orgs/openrouter-squad.yaml`): a Claude Haiku Field Marshal (via OpenRouter)
commanding a research captain (DeepSeek) with two scouts, an engineering captain (DeepSeek) with two
sappers, and a test captain (Qwen) with a tester. The soldiers are gpt-oss-120b and Qwen Flash. Each
position has a goal. There's an org-wide `orders` wiki for researched facts and a log per unit.
Captains can talk to each other, and testers can talk to sappers directly.

```bash
swarmy run --org configs/orgs/openrouter-squad.yaml --task greenhouse --budget 1
```
```bash
swarmy orders                 # how the Field Marshal planned and handed out the work
```
```bash
swarmy wiki                   # the facts the scouts recorded, and who wrote what
```

Or, in the designer, open `openrouter-squad`, choose **Greenhouse controller** in the Manoeuvres tab,
untick mock, and watch it in the run explorer. One run usually costs a few cents to about $0.30. To
compare structures on it (a lone Haiku, Haiku commanding four soldiers directly, and the squad),
use the sweep, which is capped at $3:

```bash
swarmy sweep configs/sweeps/greenhouse.yaml
```

## Order of battle (the designer)

```bash
swarmy ui [--port 8765] [--orgs-dir configs/orgs]
```

- **Chart.** Each rank is a horizontal band. A card shows the position's rank, name, model
  (coloured by family) and effort, plus counts of the wikis (▤) and contacts (⇄) it has. Drag or
  scroll to move around, Ctrl+scroll (or pinch) to zoom, and collapse any officer's command.
- **Layouts.** *Auto* draws a normal tree while it fits across the screen at a readable size. When it
  doesn't, each level is split into rows of units (each commander's direct subordinates, in a frame
  labelled with the commander), so a wide army reads top to bottom instead of shrinking to fit. You
  can also force *Tree*, *Tree, units stacked* (each unit's soldiers in a column) or *Split levels*.
- **Position tab.** Click a card to see who the position reports to, its direct subordinates, its
  rough cost per call, its wiki access and who it can contact. The rank is explained in English
  (*captain: commands a company*). You can set its standing goal and change its model, effort, limits, rank, name
  and role notes. The scope switch applies an edit to this position only, the whole level, or every
  position with the same rank. Buttons add a subordinate, duplicate the position (with its unit),
  insert an officer above it, or remove it.
- **Ranks tab.** Change how many subordinates each officer at a level commands (− / +), insert a
  rank below a level (each officer's subordinates are split among new officers, titled with the next
  rank down), remove a level, edit a whole level at once, or build a balanced army from a few
  numbers.
- **Wikis tab.** Add wikis and set their scope, who can read and write them, oversight, and whether
  the client's brief is posted on them. Selecting a wiki outlines each copy of it on the chart and
  marks who can write (solid border) and who can only read (dashed border).
- **Signals tab.** Toggle the contact rules and set the skip-level distance. Selecting a position
  draws a line to every colleague it may contact, coloured by type.
- **Manoeuvres tab.** Run the org on a task, in mock mode or with real models under a hard budget,
  and watch it live. Agents light up while they think, wait or answer; orders and reports flash along
  the edges; messages arc between cards; and an activity feed shows orders, reports, questions with
  their answers, and wiki edits as they happen.
- **Run explorer.** Inspect any run, live or past:
  - *Chain of orders*: the mission at the top, then every order each commander issued, nested under the
    order it came from, with each recipient's standing goal, the full order, how it was split further,
    and the report that came back.
  - *Wikis*: every page, who last edited it (rank, level, model), all contributors, the full revision
    history with a diff per edit, and a filter by rank.
  - *Files*: everything the run left in its workspace.
- **Models.** List endpoints (with whether their API key is set) and models, add your own endpoints and
  models, and test any model with one short real call. Additions go to `configs/models.local.yaml`.
- **Save** writes `configs/orgs/<name>.yaml`, using the compact `levels` form when the tree is
  uniform and an explicit `tree` otherwise. **YAML** shows the config and lets you paste one in.
  Undo and redo cover every edit.

The designer gets everything it shows (names, metrics, contacts, wiki membership) from the same
code the engine runs, so the chart always matches what the agents will do.

## Org configs

```yaml
# configs/orgs/regiment.yaml
name: regiment
levels:                     # top to bottom; every position in a level shares its settings
  - role: colonel           # any title; positions are named <role>-<path>, e.g. captain-1, private-1.2
    model: sonnet           # alias from configs/models.yaml, or openrouter:<id> / anthropic:<id>
    effort: low             # low | medium | high | xhigh | max  (Claude effort / OpenAI-style reasoning effort)
    goal: Keep the regiment's work tested and documented.   # optional standing objective
    span: 2                 # direct subordinates per position
  - role: captain
    model: haiku
    span: 3
  - role: private           # bottom level: span 0, does the hands-on work
    model: gpt-oss-120b
memory: persistent          # persistent: an agent remembers earlier assignments; fresh: starts clean each time
```

For uneven structures or per-position models, write an explicit `tree` instead of `levels`
(see `configs/orgs/general-staff.yaml`):

```yaml
tree:
  role: field-marshal
  model: sonnet
  goal: Split work so intelligence and engineering run in parallel.
  children:
    - role: colonel
      name: intelligence-colonel   # optional custom name (default: <role>-<path>, e.g. colonel-1)
      model: haiku
      children: [{role: scout, model: gpt-oss-120b}, {role: scout, model: "ollama:qwen3:8b"}]
    - {role: sapper, model: haiku}
```

### Ranks

The generator and the designer title positions by depth, always with the Field Marshal on top:
field-marshal, general, lieutenant-general, major-general, brigadier, colonel, lieutenant-colonel,
major, captain, lieutenant, sergeant, corporal, lance-corporal. Hands-on positions are *privates*,
and a single-agent org is a *lone wolf*. Two levels give a Field Marshal over captains, three add
colonels in between, four add generals. Inserting a layer in the designer picks the next rank
down. The list lives in `swarmy/ranks.py`.

### Goals and missions

Any position (or a whole level) can have a standing `goal`. It goes into that agent's instructions
("pursue it alongside the orders you receive"), and commanders see their subordinates' goals.
Commanders are also told their forces: each direct subordinate's rank, model and effort, and how
many positions and hands-on soldiers it commands. So the Field Marshal can give the heavy
work to the general with the bigger army or the stronger model. Set `disclose_models: false` to
hide models (useful for some experiments).

A **mission** is an open-ended objective for the top position, with an empty shared workspace and
no automatic grading:

```bash
swarmy run --org configs/orgs/field-army.yaml --budget 3 \
    --mission "Build a simulation of orbital dynamics for satellites in LEO, MEO, GEO and a Molniya orbit, comparing periods and ground tracks, with plots."
```
```bash
swarmy orders            # the chain of orders of the latest run, as an indented outline (--full for whole texts)
```

In the designer, write the mission in the Manoeuvres tab, then open **Chain of orders** to watch it
cascade down live. What gets built ends up in `results/runs/<run_id>/workspace/` and in the
explorer's Files tab.

### Knowledge sharing

```yaml
knowledge:
  wikis:
    - name: orders
      scope: org              # org | department | team
      write: managers         # who can write: all | managers (officers) | workers (hands-on)
      read: all               # who can read
      publish_brief: true     # post the client's original assignment on it
      description: Orders and plans from the staff.
    - name: unit-log
      scope: team             # one wiki per unit: an officer plus their direct subordinates
      oversight: true         # officers above the unit can read it (default)
    - name: division-file
      scope: department
      level: 1                # one wiki per level-1 position (a division), shared by everyone under it
```

Agents get `wiki_read`, `wiki_search` and (where permitted) `wiki_write` for the wikis they can
reach. Wikis are meant to be a curated knowledge base, not a log:
- Agents are told to keep one short, sourced page per topic (facts, interfaces, plan, test results).
- `wiki_write` replaces the page with an improved version unless `append` is set (for log pages).
- Every edit needs an **edit summary**, and edits that change nothing are skipped.
- Pasting orders into a page is rejected.

Every page an agent reads shows who last edited it, the last edit summary, and all contributors with
their ranks.

Every edit is kept as a revision recording the editor's name, rank, level and model, the edit
summary, the kind of edit (create, replace or append) and when in the run it happened. To read them:

- the **run explorer** in `swarmy ui` (live or past runs, with history and diffs);
- `swarmy wiki [RUN] [--wiki ID] [--page TITLE]` on the command line;
- the run directory: `runs/<run_id>/wikis/wikis.json` (everything) plus one Markdown file per page,
  with a contributor list and a revision table.

### Orders

Commanders give orders with `delegate`. By default orders are **structured**, a simplified military
orders format, so every order is a well-constrained task rather than a forwarded brief:

| field | what it holds |
|---|---|
| `objective` (required) | the single outcome this subordinate must achieve |
| `context` | only the facts it needs, distilled, in the commander's own words (or a pointer to a wiki page) |
| `deliverables` (required) | exactly what to hand back: files, facts, decisions, and in what form |
| `constraints` | interfaces, limits, rules, what not to do |
| `done_when` (required) | the test that shows the task is finished |

Orders that repeat long passages of the commander's own orders verbatim are rejected, with a message
telling it to decide the plan and pass on only what each subordinate needs. The same check stops
agents pasting orders into wikis. All of this is configurable, so it can be an experimental variable:

```yaml
orders:
  format: structured     # structured | free (a single free-text task)
  max_chars: 4000        # per order
  no_forwarding: true    # reject verbatim relaying of one's own orders
```

The run explorer's chain of orders shows each order's objective in the outline and its sections in
full. The **Signals** tab in the designer has the same settings.

### Communication rules

Assignments always flow down the tree (`delegate`) and reports flow up (`submit`). These rules add
direct contact, and all of them are off by default:

```yaml
communication:
  chain: true          # commander <-> direct subordinate, mid-assignment
  peers: true          # comrades in the same unit (same commander)
  cross_team: false    # same rank, different unit
  open: false          # anyone may contact anyone
  skip_up: 1           # may contact up to 1 rank above their own commander
  skip_down: 0         # officers may contact up to N ranks below their direct subordinates
  cc_skipped: true     # skipped officers get a copy of skip-level traffic
  modes: [ask, message]
  max_messages: 8      # per agent per run
```

- `ask` asks a colleague a question and waits for the answer. The colleague answers in a side
  conversation forked from their own history, so the answer reflects what they actually know; their
  main work isn't interrupted, and they get an FYI note about the exchange.
- `send_message` leaves a note that the recipient sees the next time they act. Notes that are never
  read are counted as `undelivered`.

Each agent's tools list exactly the colleagues it may contact, and its prompt says how each one
relates to it (commander, comrade, skip-level and so on).

Optional level fields: `max_tokens`, `max_turns`, `can_do_work` (gives managers hands-on
tools as well), `review_tools` (lets managers read the workspace and run the public tests;
on by default), and `prompt` (extra role instructions).

To see an org's tree and flatness metrics:

```bash
swarmy org configs/orgs/field-army.yaml
```

### Swapping models

```bash
# per role (or 'top' / 'bottom'), on any command
swarmy run --org configs/orgs/field-army.yaml --task code \
    --model private=openrouter:qwen/qwen3.7-flash --model field-marshal=sonnet --effort colonel=high
# any field, by dotted path
swarmy run --org configs/orgs/regiment.yaml --task qa:kind=aggregate --set levels.1.span=4 --set communication.peers=true
```

In a sweep, `models:`, `efforts:` and `set:` apply fixed overrides, and `matrix:` expands into
a Cartesian product (see `configs/sweeps/model-swap.yaml`):

```yaml
orgs:
  - path: configs/orgs/regiment.yaml
    matrix:
      private: [gpt-oss-120b, qwen-flash, haiku]
      effort.colonel: [low, medium]
```

### Endpoints and models

Models are served by **endpoints**. Built in:

| endpoint | type | key (in `.env`) |
|---|---|---|
| `anthropic` | Anthropic Messages API (streaming, adaptive thinking, effort, prompt caching) | `ANTHROPIC_API_KEY` |
| `openrouter` | OpenAI-compatible, hundreds of models, live prices and real cost per call | `OPENROUTER_API_KEY` |
| `openai` | OpenAI | `OPENAI_API_KEY` |
| `ollama` | local Ollama at `http://localhost:11434/v1` | none |
| `mock` | offline scripted models | none |

Add your own in `configs/models.yaml`, or in the designer's **Models** dialog, which writes the
git-ignored `configs/models.local.yaml`:

```yaml
endpoints:
  vllm:
    type: openai                      # any OpenAI-compatible chat-completions server
    base_url: http://gpu-box:8000/v1
    api_key_env: VLLM_API_KEY         # name of the env var holding the key; omit if none
    effort_style: none                # none | openai (reasoning_effort) | openrouter (reasoning: {effort})
    max_tokens_param: max_tokens      # or max_completion_tokens (newer OpenAI models)
  claude-gateway:
    type: anthropic                   # an Anthropic-compatible proxy or gateway
    base_url: https://llm-gateway.example.com/anthropic
    api_key_env: GATEWAY_KEY
    headers: {X-Team: research}
models:
  big-local:
    endpoint: vllm
    model: Qwen/Qwen3-32B
    price: {input: 0, output: 0}      # set real prices for paid endpoints so the budget guard works
```

Any position can also name a model inline as `<endpoint>:<model-id>` (`ollama:qwen3:8b`,
`openrouter:qwen/qwen3.7-flash`). OpenRouter prices are looked up live; local endpoints count as free;
other endpoints need a price in the catalog, or the budget guard can't limit them. Hands-on models must
support tool calling.

```bash
swarmy models endpoints          # endpoints and whether their keys are set
```
```bash
swarmy models test big-local     # one short real call to check it works
```
```bash
swarmy models check              # compare catalog prices with OpenRouter's live prices
```

### Generating structures that vary only in depth

```yaml
- generate:
    name: armee
    workers: 8              # hands-on positions, held constant
    depths: [1, 2, 3, 4]    # ranks of officers above them
    include_solo: true      # also add a lone-wolf baseline
    ladder:                 # officer tiers, top-down (omit `role` to get default ranks)
      - {role: field-marshal, model: opus, effort: max}
      - {role: general, model: opus, effort: medium}
      - {role: colonel, model: sonnet}
      - {role: captain, model: haiku}
    worker: {model: gpt-oss-120b}   # titled private unless you set a role
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
| `greenhouse` | `greenhouse` (vary `--seed`) | research a greenhouse, build a simulator and an irrigation controller, product-test against 30-day scenarios, iterate (see the worked example) | research and building in parallel, then test and fix loops |
| `mission` | `swarmy run --mission "..."` | any open-ended objective you write; ungraded | whatever the commanders decide |

All tasks are generated from the seed in the spec. The world is a fictional set of countries,
cities, companies, people and products, written up as about 110 short documents. Facts are
spread across documents (for example, a company's CEO appears only on that person's page) and
there are distractor news items. Code specs use randomized parameters, so the specs differ from
seed to seed. Grading is exact: QA answers are normalized and matched, and code is scored by the
fraction of hidden tests passed, in a subprocess.

To read a task brief and its gold answer:

```bash
swarmy task qa:kind=chain,hops=4 --seed 3 --gold
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
- `swarmy plan` warns when a level's single call could cost more than half the run cap. For
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
| `structure-control` | **does shape matter when every agent runs the same model (Haiku)?** depth 0-3 plus a company-notebook variant, 3 tasks × 3 seeds | 45 |
| `pilot` | flatness with a realistic model mix (Sonnet commander, Haiku officers, cheap privates) at 4 privates | 16 |
| `model-swap` | the regiment; privates' model × colonel's effort | 12 |
| `chain-of-command` | the full Opus→Opus→Sonnet→Haiku→private field army, flattened step by step at 8 privates | 4 |
| `greenhouse` | the greenhouse exercise with cheap OpenRouter models: lone Haiku vs Haiku + 4 soldiers vs the 3-unit squad, 2 seeds | 6 (≤ $3) |
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

- **Separate structure from model mix.** In the chain-of-command sweep, a deeper org also has more
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
  swarmy.db                       # sweeps, runs, calls, events (SQLite)
  runs/<run_id>/transcript.jsonl    # every input, step (text, thinking summary, tool calls) and tool result
  runs/<run_id>/workspace/          # final shared workspace
  runs/<run_id>/wikis/              # wikis.json (every revision) + one Markdown file per page
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
  (`swarmy models check` compares them with live prices). For OpenRouter calls, the cost the
  provider reports is what gets logged.

## Extending

- **New task family:** subclass `TaskInstance` (`swarmy/tasks/base.py`) and set `brief`,
  `grade()`, and optionally `corpus`, `workspace_files`, `public_tests()`, `progress()` and
  `key_terms`. Then register it in `swarmy/tasks/__init__.py`.
- **New provider:** implement `Provider.new_conversation()` and `Provider.step()`
  (`swarmy/providers/base.py`), then add it to `ProviderRegistry`.
