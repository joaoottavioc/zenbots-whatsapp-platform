# QA Simulation Tests — Architecture & Operations Guide

> **Audience:** Engineers and AI agents working on the conversational ordering engine. This doc is the authoritative reference for how the test system is wired, what each component does, and how to extend or debug it.
>
> **Last rewritten:** 2026-04-09

---

## 1. Why this system exists

ZenBots is a conversational ordering bot. The hardest thing to validate is **whether real customer messages get understood correctly** across the long tail of phrasing, slang, typos, and menu variation. Unit tests can't catch that — they tell you the code runs, not whether the bot understands "me arruma 1 chocolate quente tipo europa". This system fills that gap.

It is **two layers** plus a small set of supporting tools:

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2: Corpus QA (real menus from web)                       │
│  • 21 scenarios × N restaurants × M samples                     │
│  • Variance-aware: tracks mean / worst / cross-rest spread      │
│  • Two pools: 'baseline' (locked 10) or 'random' (5 of N)       │
│  • Uploaded to S3 + viewable in the corpus game frontend        │
│  • Goal: hit launch gates                                       │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────┴────────────────────────────────────┐
│  Layer 1: Hook tests (Sabor da Serra)                           │
│  • 47 deterministic tests against a hand-crafted bot            │
│  • Must always pass 47/47                                       │
│  • Auto-runs via PostToolUse hook on edits to whatsapp.py /     │
│    item_extraction.py / semantic_router.py / prompt_central.py  │
└─────────────────────────────────────────────────────────────────┘
```

The two layers serve different purposes:

| Layer | Purpose | Frequency | Pass criterion |
|---|---|---|---|
| Hook tests | **Regression guard** — block obviously broken commits | On every relevant edit | 47/47 |
| Corpus QA | **Real-world readiness** — measure how well the bot handles menus we've never seen | On demand (CLI or frontend) | Launch gates: ≥80% mean / ≥70% worst / ≤12pp spread |

---

## 2. Launch gates (the numbers that matter)

The bot is considered **launch-ready** for conversational quality when a **`pool=baseline samples=3`** corpus run clears all three:

| Gate | Threshold | What it means |
|---|---|---|
| **Mean comprehension** | **≥80%** | Average pass rate across all restaurants on comprehension scenarios (excludes greeting/suggestions/checkout — see _EASY_SCENARIOS) |
| **Worst restaurant** | **≥70%** | Floor — no customer is left behind. Excludes restaurants where everything was skipped. |
| **Cross-restaurant spread** | **≤12pp** | Standard deviation across per-restaurant comprehension rates. Variance signals that quality depends on menu shape — that is a product issue, not noise. |

The metrics live in the report at:
- `comprehension_pass_rate`
- `worst_restaurant_comp_rate` / `worst_restaurant_name`
- `cross_restaurant_spread_pp`

Always evaluate the gates against `samples=3` (or median of 3× `samples=1`) — single-sample runs have ~σ=23pp on noisy scenarios and will lie in either direction.

---

## 3. Layer 1 — Hook tests (Sabor da Serra)

### 3.1 What it is

A fictional restaurant ("Sabor da Serra") with **24 hand-crafted products** designed to stress-test specific failure modes the team has hit historically: similar names (Mignon vs Mignon ao molho cheddar), compound names (Picanha com bacon), typo-prone items, unavailable products, very short names (Big Mix, Hot Dog, Fit Wrap), and short-name false positives.

It is intentionally a **fixed bot, fixed phrases** — every single test is fully deterministic. There is no randomness, no LLM stochasticity in the inputs (the LLM still answers, but the test phrasing never changes). If a hook test fails, the cause is in the code, not the test.

### 3.2 Files

```
tests/simulation/
├── conftest.py                 # Session-scoped fixtures: creates the bot + products
├── menu.py                     # Sabor da Serra menu definition (24 products)
├── test_add_items.py           # 5 tests
├── test_remove_items.py        # 5 tests
├── test_unavailable_items.py   # 5 tests
├── test_request_suggestions.py # 5 tests
├── test_suggestion_selection.py# 9 tests
├── test_suggestion_to_order.py # 5 tests
├── test_short_name_products.py # 5 tests
└── test_checkout_protection.py # 8 tests   (47 total)
```

### 3.3 How to run manually

```bash
# Full baseline
docker compose exec backend pytest tests/simulation/ -v \
  --ignore=tests/simulation/test_multi_restaurant.py \
  --ignore=tests/simulation/test_real_restaurants.py \
  --ignore=tests/simulation/test_real_restaurants_v2.py \
  --ignore=tests/simulation/test_random_restaurants.py

# Single file
docker compose exec backend pytest tests/simulation/test_add_items.py -v
```

### 3.4 When it runs automatically

The PostToolUse hook at `.claude/hooks/post-edit-simulation.py` triggers the full 47-test suite whenever any of these files are edited:

- `app/whatsapp.py`
- `app/item_extraction.py`
- `app/semantic_router.py`
- `app/prompt_central.py`

Reports are written to `.claude/simulation-reports/{timestamp}_PASS_or_FAIL_{filename}.txt`.

**Result:** must always be 47/47. A single hook test failure means a code regression — investigate before continuing any other work.

### 3.5 How to add a new hook test

1. Add the test to the relevant `tests/simulation/test_*.py` file using existing patterns.
2. If a new product is needed, edit `tests/simulation/menu.py`.
3. Verify locally by running the file.
4. Update the count in `.claude/hooks/post-edit-simulation.py` if it asserts a specific total.

Hook tests are the **regression contract**. Only add tests that you want the project to forever guarantee.

---

## 4. Layer 2 — Corpus QA

This is the larger system. It validates the bot against **real restaurant menus** harvested from the web, runs **21 scenarios** per restaurant, and reports per-restaurant per-scenario pass/fail with variance-aware aggregation.

### 4.1 Architecture overview

```
                          ┌──────────────────────────────────┐
                          │  Frontend: corpus game tab       │
                          │  (admin-only)                    │
                          │  - "Run QA Tests" button         │
                          │  - pool selector (baseline/random)│
                          │  - samples selector              │
                          │  - latest report viewer          │
                          └────────────┬─────────────────────┘
                                       │ POST /admin/corpus/run-tests
                                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  app/monitoring_routes.py                                        │
│  - Acquires Redis single-run lock (qa_lock, TTL=60min)           │
│  - Spawns: python tests/simulation/corpus/run_corpus_qa.py       │
│           --samples=N --pool=POOL                                │
│  - Fire-and-forget: returns HTTP 202 immediately                 │
│  - stdout/stderr → .qa_run_output.log (no pipe deadlock)         │
│  - start_new_session=True (survives uvicorn --reload)            │
│  - Subprocess releases lock when done                            │
│  - Frontend polls GET .../status (includes log_tail)             │
└────────────┬─────────────────────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────────────────────┐
│  tests/simulation/corpus/run_corpus_qa.py                        │
│  1. Pre-run cleanup (delete orphan rand-test-* bots)             │
│  2. Pick restaurants from manifest.json (baseline or random)     │
│  3. For each restaurant:                                         │
│     - POST /bots, POST /bots/{id}/products from extractions/     │
│     - random.sample(non_drink[:10], 2) → mark unavailable        │
│     - gen_scenarios(products, unavail_idx) → 21 scenarios        │
│     - Insert subscription so the bot is "authorized"             │
│  4. Write _random_test_data.json                                 │
│  5. For sample in 1..N:                                          │
│        docker compose exec backend pytest                        │
│            tests/simulation/test_random_restaurants.py -v        │
│  6. Parse pytest output → per-sample report                      │
│  7. _merge_sample_reports → final aggregate                      │
│  8. Upload to S3 (qa-reports/dev/runs/{timestamp}.json)          │
│  9. Cleanup all bots created in this run                         │
└──────────────────────────────────────────────────────────────────┘
```

### 4.2 Component map

```
tests/simulation/corpus/
├── images/                     Real menu photos (jpg/png), source-of-truth
├── manifest.json               Index: id, file, category, restaurant_name,
│                                source_url, product_count, validated, menu_type
├── extractions/menu_NNNN.json  Cached gpt-4o vision extractions per image
├── rejected/                   Images that failed validation (<5 products)
├── pending_images.json         ~2000 candidate URLs (DuckDuckGo) for the game
├── classifier_game.html        Browser UI for human image acceptance
├── classifier_server.py        Game backend (downloads accepted images)
├── validate_corpus.py          Sends new images through Cadastro Mágico
├── pick_baseline_pool.py       One-off: pick + commit the locked baseline_pool.json
├── baseline_pool.json          The 10 restaurants used for clean per-fix attribution
├── phrase_bank.py              Phrase templates for generating customer messages
└── run_corpus_qa.py            The runner

tests/simulation/
├── _random_test_data.json      Written by the runner; read by pytest. Maps
│                                each restaurant label to its bot_id, phone_number_id,
│                                and the 21 scenario phrases/products.
├── test_random_restaurants.py  21 pytest classes (one per scenario), each
│                                parametrized over the labels in _random_test_data.json
├── conftest.py                 Sabor da Serra fixtures (Layer 1 only)
└── menu.py                     Sabor da Serra menu definition (Layer 1 only)

app/
├── monitoring_routes.py        /admin/corpus/* endpoints
├── qa_lock.py                  Redis single-run lock (Phase 1A)
├── qa_reports.py               S3 upload/list/get for finished reports
└── item_extraction.py          extract_items_with_quantities + _STOP set

frontend/                       Corpus game tab (UI)
```

### 4.3 The 21 scenarios

Each scenario is one pytest test class. They are **parametrized over the restaurant set**, so a single run with 10 restaurants and 1 sample produces 10 × 21 = **210 test invocations**.

Comprehension metric **excludes** `greeting`, `suggestions`, and `checkout` — those are easy/state-machine, not natural-language understanding. The remaining 18 scenarios drive the gate metrics.

| # | Test class | Scenario name | Comprehension? | What it tests |
|---|---|---|---|---|
| 1 | TestRandAddSingle | `add_single` | yes | One product, qty=1, basic ADD intent |
| 2 | TestRandAddMulti | `add_multi` | yes | Two products, one with qty=2 — multi-item parse + qty assignment |
| 3 | TestRandContinuation | `continuation` | yes | Verb-less follow-up after an add ("e uma cocacola") — production's #1 pattern |
| 4 | TestRandUnavailable | `unavailable` | yes | One available + one unavailable in same message — em falta detection |
| 5 | TestRandRemove | `remove` | yes | Cart removal by name |
| 6 | TestRandSuggestions | `suggestions` | **no** (easy) | "o que tem de bom?" → bot shows list, doesn't add anything |
| 7 | TestRandCheckout | `checkout` | **no** (easy) | Add product, finalize, navigate the checkout state machine |
| 8 | TestRandGreeting | `greeting` | **no** (easy) | "oi" → friendly response, no cart changes |
| 9 | TestRandAbbreviation | `abbreviation` | yes | Order by abbreviated/short name ("brahma" for "Cerveja Brahma 600ml") |
| 10 | TestRandDoubleAdd | `double_add` | yes | Same product added twice → qty merges to 2, not creates duplicate row |
| 11 | TestRandQuestion | `question` | yes | "quanto custa o X?" mid-order — answer, do not add |
| 12 | TestRandAddRemove | `add_remove` | yes | Add then remove same product — cart should be empty |
| 13 | TestRandSuggestionTrapQuestion | `trap_question` | yes | Suggestion offered, customer asks a question → answer, don't add |
| 14 | TestRandSuggestionTrapClear | `trap_clear` | yes | Suggestion offered, customer says "limpa tudo" → clear cart (F4 target) |
| 15 | TestRandSuggestionTrapUnrelatedAdd | `trap_unrelated_add` | yes | Suggestion offered, customer adds an unrelated product → add the product, don't misread as selection (F5 target) |
| 16 | TestRandSuggestionTrapFinish | `trap_finish` | yes | Suggestion offered, customer wants to finalize → finalize, don't misread as selection (F1 target) |
| 17 | TestRandRemoveSubsetByName | `subset_remove` | yes | Cart has [A×2, B×1]; "tira o B" → cart should be [A×2] |
| 18 | TestRandRemoveMultipleAtOnce | `multi_remove` | yes | Cart has 3 items; "tira A e B" → cart should have just C |
| 19 | TestRandQtyReduction | `qty_reduction` | yes | Cart has [A×3, B×2]; "deixa só 1 A" → cart should be [A×1, B×2] (F2 target) |
| 20 | TestRandMultiTurnFlow | `multiturn_flow` | yes | 9-step happy path: greet → suggest → add → continue → question → remove → finalize → entrega → original item still present |
| 21 | TestRandQuantityInProductName | `digit_in_name` | yes | "quero um pizza 4 queijos" — product NAMES with digits ("4") shouldn't be parsed as qty. **Auto-skips if no digit-in-name product exists in the bot's menu.** |

### 4.4 Restaurant selection (pools)

The runner selects which restaurants to test against via `--pool` (or `CORPUS_POOL` env var or `pool` query param):

| Pool | Source | Size | Purpose |
|---|---|---|---|
| `baseline` | `tests/simulation/corpus/baseline_pool.json` (locked) | **10** | **Clean per-fix attribution.** Same restaurants every run, so deltas are real not sampling lottery. **This is the pool the launch gates apply to.** |
| `random` | 5 random validated entries from `manifest.json` | 5 | Broad-baseline check. Catches population drift. Uses different restaurants every run. **Headline numbers are not directly comparable run-to-run.** |

The `baseline_pool.json` shape:
```json
{
  "version": 1,
  "size": 10,
  "category_counts": { "padaria": 2, "cafeteria": 2, ... },
  "entries": [
    { "id": "menu_0045", "category": "padaria", "restaurant_name": "...",
      "menu_type": "template", "product_count": 12 },
    ...
  ]
}
```

To rebuild the baseline pool (only when the validated corpus has changed substantially), run:
```bash
python tests/simulation/corpus/pick_baseline_pool.py
```

Then commit the resulting `baseline_pool.json`. The pool is committed to git on purpose — it must be stable across runs.

### 4.5 Sample size (`--samples`)

Each sample re-runs the **entire pytest suite** against the same `_random_test_data.json` (so the same scenarios, same products, same phrases) but with a **fresh contact phone number**, which gives a fresh conversation. Independent samples capture **LLM stochasticity**.

```bash
python tests/simulation/corpus/run_corpus_qa.py --samples=3 --pool=baseline
```

| samples | Purpose | Time (10 rests) |
|---|---|---|
| **1** | Fast feedback after a fix. Use when iterating on a regex change and you want to know if a target scenario moved. Headline numbers are noisy. | ~5 min |
| **3** | Standard. Median per-scenario. The cell aggregation uses **conservative passing** — a cell is green only if ALL samples passed. | ~15-20 min |
| **5+** | Deep diagnostic. For pinning down a flaky scenario or estimating the true mean. | ~25-50 min |

**The launch gates apply to `samples=3` (or median of 3× `samples=1`).**

### 4.6 Single-run lock (Phase 1A)

`app/qa_lock.py` is a Redis-backed single-owner lock (TTL=60 min). The flow:

1. Frontend POST → `/admin/corpus/run-tests` calls `qa_lock.try_acquire()`. Returns 409 if already held.
2. The endpoint passes `CORPUS_LOCK_OWNED=1` + `CORPUS_LOCK_OWNER_ID=...` to the subprocess env and returns **HTTP 202 immediately** (fire-and-forget).
3. The subprocess adopts the lock's `owner_id` and **releases it in its own `finally` block** when done. The endpoint does NOT release — the subprocess is the sole owner.
4. `release()` is **conditional** — it only deletes the key if the current holder still matches `owner_id` (so a TTL-expired-then-reacquired lock from another caller does not get clobbered).
5. The subprocess runs with `start_new_session=True` and stdout/stderr redirected to `.qa_run_output.log` — it survives uvicorn `--reload` restarts and has no pipe-deadlock risk.

The frontend polls `GET /admin/corpus/run-tests/status` to disable the Run button while a sibling tab, a CLI run, or a previous click is still in flight. The status endpoint returns `log_tail` (last 4KB of the run output) for live progress.

CLI users acquire/release the same lock via the script's own `try_acquire`/`release` if `CORPUS_LOCK_OWNED` is not set.

### 4.7 Bot lifecycle

For each restaurant, the runner:

1. **Creates the bot**: `POST /bots` with a unique `phone_number_id` like `rand-test-7-1775761241`.
2. **Loads products**: for each product in `extractions/menu_NNNN.json`, `POST /bots/{id}/products` with name, price, category, description.
3. **Marks 2 random products as unavailable**: `random.sample(non_drink[:10], min(2, len(non_drink)))`. This is the "natural unavailability" any real restaurant has, used by the `unavailable` test scenario.
4. **Generates scenarios** via `gen_scenarios(products, unavail_idx)`. See section 4.8.
5. **Inserts a subscription** so the bot is `authorized` (skips paywall checks).

After the pytest invocations finish, all created bots are deleted via `cleanup_bots`. The cleanup runs in a `finally` block so it executes even on KeyboardInterrupt or test crashes.

### 4.8 Scenario generation: `gen_scenarios(products, unavail_indices)`

Defined in `tests/simulation/corpus/run_corpus_qa.py:79`. Builds the dict that gets serialized into `_random_test_data.json`.

Key picks (via `pick_random_products`):
- `single` — one product, used by `add_single`, `add_remove`, `continuation_first`, `multiturn_flow setup`
- `multi_1`, `multi_2` — two products, used by `add_multi`, `subset_remove`, `multi_remove`, `qty_reduction`
- `remove_item` — used by `remove`, `multi_remove`
- `checkout_item` — used by `checkout`
- `question_item` — used by `question`, `trap_question`
- `double_add_item` — used by `double_add`
- `unavail_prod` + `avail_with` — used by `unavailable`

Phrases are rendered via `format_add(name, qty)`, `format_remove(name)`, `format_question(name)`, `format_suggestion()`, `format_greeting()` — all in `phrase_bank.py`.

**Critical reuse:** several scenarios reuse the same phrase as setup. If `add_multi_msg` is broken, then `subset_remove`, `multi_remove`, and `multiturn_flow` fail too because they call `await c.send(sc["add_multi_msg"])` as setup. This is by design — composite tests catch integration issues — but it means a single phrase bug can cascade-fail multiple scenarios for the same restaurant.

### 4.9 The phrase bank (`phrase_bank.py`)

Templates for rendering customer messages. Three tiers controlled by `PHRASE_TIER` env var: `clean`, `slang`, `mixed` (default), `hard` (mixed + typos).

```python
ADD_PHRASES = [
    ("quero {qty} {name}", 0.12),
    ("me arruma {qty} {name}", 0.06),
    ("{qty} {name}", 0.08),
    # ... weighted templates
]
SLANG_ADD_PHRASES = [
    ("manda {qty} {name} ae", 0.10),
    ("rola {qty} {name}", 0.03),
    # ...
]
MIXED_ADD_PHRASES = [(p, w * 0.55) for p, w in ADD_PHRASES] + [
    (p, w * 0.45) for p, w in SLANG_ADD_PHRASES
]
```

`format_add(name, qty)` picks a weighted template, fills in `{name}` and `{qty}`, and returns the rendered string. **When `qty > 1`, the pool is filtered** to templates that contain `{qty}` AND don't hardcode `um|uma|uns|umas` — without that filter `str.format` silently drops the qty kwarg on a `{name}`-only template, the rendered phrase says "taça tentação" instead of "2 taça tentação", the test asserts qty=2, and the scenario fails through no fault of the bot. Discovered 2026-04-09 when Doce Café cascade-failed `add_multi`, `subset_remove`, `multi_remove`, and `multiturn_flow` because all of them reuse `add_multi_msg`.

**Adding a new phrase template:**
1. Add it to the appropriate list (`ADD_PHRASES`, `SLANG_ADD_PHRASES`, etc.) with a weight.
2. **Always include `{qty}` if it's an ADD template you expect to be called with `qty > 1`.** If you genuinely want a qty-less template (like `("vou de {name}", 0.04)`), it will only be picked when the caller passes `qty=1`.
3. Make sure the template can be parsed by `extract_items_with_quantities` — i.e. don't introduce filler words that would leak into product names. See section 4.10.
4. Re-run `samples=3` to confirm no scenario regressed.

**REMOVE / QUESTION / SUGGESTION pools** don't take `qty`, so they're unaffected by the qty-drop fix.

### 4.10 Item extraction (`app/item_extraction.py`)

`extract_items_with_quantities(text)` is the regex-based parser used by the **pre-router guards** (and several other paths) to break a customer message into `[(qty, item_name), ...]` tuples. It's word-based and uses a `_STOP` set of stopwords + qty-word splitters.

Why it matters for tests: when the phrase bank uses an informal verb like "rola", "joga", "solta", "arruma", that verb has to be in `_STOP`, otherwise the parser leaks it into the item name. Example before the 2026-04-09 fix:

```python
extract_items_with_quantities("rola um chocolate quente tipo europa")
# WRONG: [(1, 'rola'), (1, 'chocolate quente tipo europa')]
```

After adding `"rola"` to `_STOP`:
```python
# CORRECT: [(1, 'chocolate quente tipo europa')]
```

**Adding a new informal verb to a phrase template requires adding the verb stem to `_STOP`.** If you skip this step, expect spurious failures on `continuation`, `subset_remove`, and any other test using extracted items downstream.

The `_STOP` set lives at `app/item_extraction.py:16`, alphabetically scattered. Search for the "Informal ADD verbs" comment block (around line 132) and add the new verb there.

### 4.11 Pre-router guards (F1-F5) and how they relate to scenarios

The bot's `resolve_intent()` function in `app/whatsapp.py` runs **deterministic pre-router guards** before the semantic embedding router. Each guard is a regex that maps a known phrase shape to an intent, bypassing the noisier embedding lookup. They were added in response to specific scenario failures discovered through corpus runs:

| Fix | Regex constant | Pattern | Routes to | Fixed scenario |
|---|---|---|---|---|
| F1 | `_FINISH_KEYWORD_RE` (and `clear_pending` block) | "vamo finalizar / vou fechar / pode fechar pedido" — verb-prefixed bare-fechar variants | `FINISH_ORDER` | `trap_finish` (also slang `finalizar`) |
| F2 | `_QTY_REDUCE_RE` | "deixa só N X / fica só N X / muda pra N X" | `MODIFY` (with `set_mode=True` in `_programmatic_cart_reduce`, so qty becomes N not delta-N) | `qty_reduction` |
| F3 | `_REMOVE_KEYWORD_RE` / `_REMOVE_ALT_RE` / `_REMOVE_NEGATION_RE` | "tira N X / cancela esse X / não manda o X" | `MODIFY` (REMOVE) | `remove`, `subset_remove`, `multi_remove` |
| F4 | `_CLEAR_KEYWORD_RE` | "(limpa\|esvazia\|zera\|apaga) (tudo\|td\|carrinho\|pedido)" | `CLEAR_CART` | `trap_clear` |
| F5 | suggestion-handler verb-prefix bypass (Part A/B/C in the pre-handler section) | clears `cart.last_suggestions` BEFORE the suggestion handler runs when the message has an ADD verb (with no selection signal), a QTY_REDUCE pattern, or a CLEAR pattern | normal flow | `trap_unrelated_add`, partial `trap_question`, ensures F2 and F4 reach `resolve_intent` |

Other deterministic guards in the same file: `_ADD_KEYWORD_RE`, `_BARE_ADD_RE`, `_CONTINUATION_KEYWORD_RE`. They all sit at the top of `resolve_intent()` and are placed in a specific precedence order — see the inline comments in `whatsapp.py`.

**When investigating a test failure**, check first whether a pre-router guard SHOULD have fired and didn't. The `[INTENT] pre-router XXX guard matched` log line is your friend.

### 4.12 The conversation runner inside tests

Each pytest test class instantiates a `RandSimContext` (in `test_random_restaurants.py`) which:

1. Generates a fresh phone number per test (`5500{uuid_int[:10]}`).
2. Monkey-patches `app.whatsapp.send_whatsapp_message` so outgoing WhatsApp calls are captured locally instead of hitting the real Meta API.
3. Calls `app.worker.process_whatsapp_message(ctx, payload)` directly with a synthetic webhook payload — same code path as the production worker.
4. Reads the resulting cart state from the DB via `get_cart_items()` for assertions.

This means **the test goes through the real production code path** — webhook handler, intent classifier, LLM tool calling, DB writes — except for the outbound WhatsApp send. Anything that runs in production runs here.

### 4.13 Pytest invocation and timeout

The runner shells out to docker:
```python
test_cmd = [
    "docker", "compose", "exec", "-T", "backend",
    "pytest", "tests/simulation/test_random_restaurants.py", "-v", "--tb=short",
]
```

The per-pytest-invocation timeout scales with restaurant count to avoid the historical 600s ceiling tripping on baseline-pool runs:

```python
n_tests_per_sample = len(bots) * len(_SCENARIO_MAP)  # e.g. 10 * 21 = 210
pytest_timeout = max(600, n_tests_per_sample * 6 + 60)  # ~22 min for 10 rests
```

If a sample times out, the runner catches `subprocess.TimeoutExpired`, logs it, and the merge step fills in zeros (which Phase 1A integrity validation will then flag as `integrity != "ok"`).

### 4.14 Report aggregation (`_merge_sample_reports`)

Defined at `tests/simulation/corpus/run_corpus_qa.py:888`. Rules:

- **Headline counts** (`passed`/`failed`/`skipped`/`comprehension_*`) are **summed** across samples. So a 3-sample 10-restaurant run with 21 scenarios = 210 attempts per sample → **630 total comprehension attempts** after merge.
- **Per-restaurant per-scenario cells** get `passed_count` / `total_count` / `pass_rate` / `samples` (list of statuses) / `passed` / `status`. The `passed` field is **conservative**: a cell is green only if ALL samples passed. This keeps the frontend cells from misleading users when a scenario is intermittent.
- **Worst-restaurant** and **cross-restaurant spread** are computed across per-restaurant comprehension rates (excluding restaurants where everything was skipped).
- **`failure_summary`** is `failure_lines[-30:]` joined — the last 30 fail lines from pytest output. If a restaurant has more than 30 fails total in one run, the earliest ones get truncated. Use the per-scenario `samples` list to find the truth.
- **`integrity`** is set to `"ok"` if every restaurant has at least one comprehension attempt logged. Otherwise the report is flagged with `integrity_warning` describing what's missing.

### 4.15 Report storage (`app/qa_reports.py`)

Aggregated reports are uploaded to S3:
```
s3://{AWS_BUCKET_NAME}/qa-reports/{env}/runs/{timestamp}.json
```

`env` is `dev` or `prod` based on the `ENVIRONMENT` env var (defaults to `dev`).

API:
- `upload_qa_report(report) -> str | None` — uploads, returns S3 key or `None`
- `list_qa_reports(limit=50) -> list[dict]` — newest-first, paginates server-side then sorts by `LastModified`. **Important:** an earlier bug used `MaxKeys=limit` which truncated server-side in lexicographic key order, returning only the OLDEST `limit` reports. Fixed by paginating first.
- `get_qa_report(key) -> dict | None`

If the AWS bucket isn't configured (no `AWS_BUCKET_NAME`), the runner falls back to writing the report locally at `.claude/simulation-reports/runs/corpus_{timestamp}.json`.

### 4.16 Frontend (corpus game tab)

The frontend has an admin-only "Corpus QA" tab that wraps:

- **Run QA Tests** button → POST `/admin/corpus/run-tests?samples=N&pool=POOL`
- **In-progress polling** → GET `/admin/corpus/run-tests/status` (every few seconds while a run is active). Disables the Run button when another tab/CLI/click is still running.
- **Latest report viewer** → list & display reports from `list_qa_reports` / `get_qa_report`. Shows headline 3-box (mean / worst / spread), per-restaurant comprehension table, per-scenario fail tally.
- **Pool selector** (`baseline` / `random`)
- **Samples selector** (1 / 3 / 5)
- **Image classifier game** (separate sub-tab) — see section 5.

---

## 5. Building and growing the corpus

The corpus grows in three steps. Each step has a Claude Code slash command (`.claude/commands/corpus-game.md`, `corpus-validate.md`, `corpus-test.md`) but you can also run them directly.

### 5.1 Step 1 — Collect images (`/corpus-game`)

```bash
python tests/simulation/corpus/classifier_server.py
# → open http://localhost:8888/classifier_game.html
```

Browser-based image classifier game:
- **→ or Enter** = accept image (downloads it to `corpus/images/`, adds entry to `manifest.json` with `validated: false`)
- **← or S** = skip
- **Z** = undo last action
- Each "level" = 30 accepted images
- Progress auto-saves to browser localStorage

Source images come from `pending_images.json` (~2000 candidate URLs scraped from DuckDuckGo image search).

**Cost:** free, takes ~10-15 minutes to accept 20 images.

### 5.2 Step 2 — Validate (`/corpus-validate`)

```bash
python tests/simulation/corpus/validate_corpus.py
```

Sends each unvalidated image through `POST /bots/{id}/catalog/upload-from-file`, which uses gpt-4o vision to extract product names, prices, and categories.

- Images with **≥5 products** → marked `validated: true` in `manifest.json`, extraction saved to `extractions/menu_NNNN.json`.
- Images with **<5 products** → moved to `corpus/rejected/`.
- Category is auto-detected from the product names.
- Each entry gets a `menu_type` field: `template` (synthetic-looking cardapio screenshots) or `tier2` (real iFood/Rappi/Google Maps screenshots).

**Cost:** ~$0.03 per image (one gpt-4o vision call). 30 images = ~$0.90.

**Important:** the runner logs a loud WARNING if 100% of validated entries are `template`. Until you have meaningful `tier2` coverage, headline numbers OVERSTATE real-world readiness — see the `Tier 2` warning in the runner output.

### 5.3 Step 3 — Run tests (`/corpus-test`)

```bash
# Random pool, single sample (fastest)
python tests/simulation/corpus/run_corpus_qa.py

# Locked baseline pool, 3 samples (the launch-gate run)
python tests/simulation/corpus/run_corpus_qa.py --pool=baseline --samples=3

# Or via the frontend admin tab
```

### 5.4 Picking the baseline pool

```bash
python tests/simulation/corpus/pick_baseline_pool.py
```

Picks 10 restaurants spread across categories from the validated manifest. The output is committed to `tests/simulation/corpus/baseline_pool.json`. Re-run only when you've added enough new corpus to want a refresh — the whole point of the locked pool is that it stays locked.

---

## 6. Variance — sources, estimates, and what to do about them

The corpus QA system has multiple sources of run-to-run variance. Knowing which is which is essential for interpreting results.

### 6.1 LLM stochasticity

Same exact prompt to gpt-4o-mini can produce different tool-calling outputs. Empirically: ~5-10% per-scenario flip rate. **Mitigation:** `samples >= 3`. Each sample is an independent LLM roll; the merged cell is conservatively passing only if all samples passed.

### 6.2 Random unavailability picking

The runner does `random.sample(non_drink[:10], 2)` — different products get marked unavailable each run. If a product used by `add_single` happens to be picked unavailable, that scenario will fail legitimately (the bot correctly refuses to add it, but the test was generated assuming it'd be available).

**Mitigation:** the picks are guaranteed not to be ones the test scenarios depend on, because `pick_random_products` only picks `single`/`multi_1`/`multi_2`/etc. from the AVAILABLE pool. So **as written this should not actually contribute variance** — but if you're investigating an outlier failure on add_single, double-check the scenario data in `_random_test_data.json` against the unavailable picks logged at setup time.

### 6.3 Phrase template variance

Every run draws fresh phrases from the phrase bank. Different templates have different difficulty. Two runs against the same restaurant can fail different scenarios just because of which template was drawn.

**Mitigation:** if you observe a single-restaurant outlier (a la Doce Café 2026-04-09), inspect `_random_test_data.json` for that restaurant before assuming it's a real bug. Investigate template draws first.

### 6.4 Product extraction variance

The same image can produce slightly different product lists across runs of `validate_corpus.py` (gpt-4o vision is stochastic). **Mitigation:** extractions are cached in `extractions/menu_NNNN.json`. The runner reads the cache; it does not re-extract on each run. Re-extraction only happens when you re-run `validate_corpus.py`.

### 6.5 Population variance (random pool only)

The `random` pool picks 5 different restaurants every run. Headline numbers are not directly comparable across `random` runs.

**Mitigation:** use `pool=baseline` for any comparison work. Use `pool=random` only for broad-baseline drift checks.

### 6.6 Empirical variance

From the bot intelligence backlog: σ on noisy scenarios (`subset_remove`, `trap_finish`, `multiturn_flow`, `trap_clear`) is **~23pp** at samples=1. To detect a +20pp lift on a σ=23pp scenario at 80% statistical power, you need **~22 samples per arm**. We have 1-3. **Single-sample baseline runs are at or below the noise floor for clean attribution of small fixes.**

In practical terms: a +5pp move at samples=1 is noise. A +15pp move at samples=3 is real. The **pattern** of failures matters more than the headline number when iterating.

---

## 7. How to manually edit the test system

### 7.1 Add a new scenario

Adding a 22nd scenario requires three coordinated edits:

1. **`tests/simulation/corpus/run_corpus_qa.py:gen_scenarios`** — add the new fields to the returned `sc` dict (e.g. `my_new_msg`, `my_new_expected`).
2. **`tests/simulation/corpus/run_corpus_qa.py:_SCENARIO_MAP`** — add the new test class name → scenario name mapping.
3. **`tests/simulation/test_random_restaurants.py`** — add a new `class TestRandMyNewThing` with an async parametrized test that reads `sc["my_new_msg"]` and asserts on the cart state.

If your scenario should NOT count toward comprehension, also add its name to `_EASY_SCENARIOS`.

After editing, run a `samples=1 pool=random` baseline first to confirm the scenario runs at all, then `samples=3 pool=baseline` for a real reading.

### 7.2 Add a new phrase template

In `tests/simulation/corpus/phrase_bank.py`, find the appropriate list (`ADD_PHRASES`, `SLANG_ADD_PHRASES`, `REMOVE_PHRASES`, etc.) and add `(template, weight)`. Two rules:

1. **ADD templates that should support `qty > 1` MUST contain `{qty}`** and MUST NOT hardcode `um|uma|uns|umas`. The runtime filter in `format_add` enforces this.
2. **Any verb in the template must be parseable by `extract_items_with_quantities`** — meaning the verb stem must be in `_STOP` (`app/item_extraction.py:16`). If the verb is not a stopword, it leaks into the extracted item name.

### 7.3 Update the stopword list

In `app/item_extraction.py`, the `_STOP` frozenset starts at line 16. The "Informal ADD verbs" comment block (around line 132) is the natural home for new verb stopwords. Add the bare verb stem (e.g. `"rola"`, not `"rolar"`).

After editing:
1. Verify the regression with: `extract_items_with_quantities("rola um café")` → `[(1, 'café')]`
2. The PostToolUse hook will auto-run the 47 hook tests and write a PASS/FAIL report to `.claude/simulation-reports/`. They must still be 47/47.

### 7.4 Add a restaurant to the baseline pool

Either:
- **Recommended:** re-run `pick_baseline_pool.py` to regenerate the whole locked pool. This is the right call when the validated corpus has grown enough to want refreshed coverage.
- **Manual:** edit `baseline_pool.json` and add an entry. The `id` MUST match a validated entry in `manifest.json`. The runner skips pool entries that don't exist in the manifest with a WARNING line.

After editing, commit the new `baseline_pool.json`.

### 7.5 Tweak launch gates

The launch gates aren't hardcoded in the runner — they're documented in this file (section 2) and applied by humans when reading reports. If you want to enforce them programmatically (e.g. fail CI when gates aren't met), add a check after `_merge_sample_reports` in `_main_locked` that compares `comprehension_pass_rate`, `worst_restaurant_comp_rate`, and `cross_restaurant_spread_pp` against the thresholds and exits non-zero. The numbers are all already in the `report` dict.

### 7.6 Extend the runner CLI

Both `--samples` and `--pool` flow through `_get_cli_args()` and accept env-var fallbacks (`CORPUS_SAMPLES`, `CORPUS_POOL`). To add a new flag:

1. Add it to the `argparse.ArgumentParser` in `_get_cli_args`.
2. Add a corresponding `_get_my_flag()` helper with the same precedence (CLI > env var > default).
3. Add it to the API signature in `app/monitoring_routes.py:admin_corpus_run_tests` so the frontend can pass it.
4. Update the frontend to expose it.

### 7.7 Re-run the corpus from scratch

**You almost never need to do this.** Extractions are cached on disk. The only reasons to re-run `validate_corpus.py`:

- The Cadastro Mágico extractor changed in a way that affects product names/categories.
- You want to recompute `category` auto-detection.
- A specific extraction is bad and needs a re-do (in which case just delete the one `extractions/menu_NNNN.json` file and re-run; it'll skip already-validated entries).

---

## 8. How to investigate a failing scenario

Worked example: 2026-04-09, Doce Café cascade-failed 9 scenarios on a `pool=baseline samples=1` run while the other 9 restaurants were fine. The headline was 75.6% mean and 41.2% worst-case.

**Step 1 — Pull the report and locate the outlier.**
```python
docker compose exec -T backend python -c "
from app.qa_reports import list_qa_reports, get_qa_report
import json
r = get_qa_report(list_qa_reports(limit=1)[0]['key'])
# print headline + per-restaurant comprehension
"
```

**Step 2 — Inspect the failing restaurant's per-scenario detail.**
```python
rest = next(x for x in r['restaurants'] if 'Doce' in x['name'])
fails = [s for s, d in rest['scenarios'].items() if not d.get('passed')]
print(fails)
# → ['add_single', 'add_multi', 'continuation', 'remove', 'checkout',
#    'add_remove', 'trap_question', 'trap_clear', 'trap_finish',
#    'subset_remove', 'multiturn_flow', 'digit_in_name']
```

**Step 3 — Pull the actual phrases that this restaurant got this run.**
```bash
docker compose exec -T backend python -c "
import json
data = json.loads(open('/code/tests/simulation/_random_test_data.json').read())
for label, b in data.items():
    if 'Doce' in b['name']:
        for k, v in b['scenarios'].items():
            print(f'{k:<28} {v!r}')
"
```

**Step 4 — Look for anomalies in the phrases.** In this case the smoking gun was:
```
add_multi_msg  'taça tentação e manda 1 frappe de vanilla ae'
add_multi_expected  [['TAÇA TENTAÇÃO', 2], ['FRAPPE DE VANILLA', 1]]
```
The phrase has no "2" for taça tentação, but the expected qty is 2. Mismatch.

**Step 5 — Trace to root cause.** The phrase was rendered by `format_add('TAÇA TENTAÇÃO', qty=2)`. Reading `format_add` in `phrase_bank.py`, the bug was visible: `format_add` did `phrase.format(name=..., qty=qty_str)` against a randomly-picked template, but several SLANG_ADD_PHRASES templates were `{name}`-only (no `{qty}` placeholder). `str.format` silently ignores the unused kwarg → qty dropped → test fails through no fault of the bot.

**Step 6 — Verify cascading.** `add_multi_msg` is reused as setup by `subset_remove`, `multi_remove`, `multiturn_flow`. So one bad template draw broke 4+ scenarios for this restaurant.

**Step 7 — Fix and verify.** Filtered the pool in `format_add` to only `{qty}`-bearing templates when `qty > 1`. Re-ran `samples=1 pool=baseline` → Doce Café bounced from 9/21 to 17/21, mean from 75.6% to 82.2%, worst from 41.2% to 76.5%, spread from 15.6pp to 6.1pp. All three launch gates met.

This investigation methodology generalizes:

1. **Pull the report. Identify the outlier.** Don't average a single bad restaurant into "mean dropped".
2. **Read the actual rendered phrases for that restaurant.** Don't trust your assumptions about what `format_add` produced.
3. **Reproduce the extraction or pre-router behavior locally** for the suspect phrase. `extract_items_with_quantities`, `_QTY_REDUCE_RE.search`, etc. are easy to call from a Python REPL inside the backend container.
4. **Distinguish bot bug vs test infrastructure bug vs LLM stochasticity.** The first deserves a code fix. The second deserves a phrase bank or runner fix. The third deserves more samples. They are NOT the same thing and confusing them wastes time.

---

## 9. Limits and known weaknesses

### 9.1 Gold-standard limits

- **The corpus is mostly `template` images** (synthetic-looking cardapio screenshots). Real-world signal requires `tier2` (iFood/Rappi/Google Maps screenshots). Until we have meaningful `tier2` coverage, the launch gates measure a **proxy** for production readiness, not the real thing. The runner logs a loud warning when 100% of the corpus is `template`.
- **Phrase bank is hand-built.** It approximates Brazilian Portuguese WhatsApp ordering patterns but is not data-driven. Real customers will produce phrasings the bank doesn't cover.
- **Single-tier LLM assertions.** The tests check the cart state (DB rows), not the bot's reply text. A bot that adds the right product but says something weird in the response will pass the test.
- **No conversation drift testing.** Tests are short-horizon (1-9 messages). Real customers loop 20+ messages.

### 9.2 Test infrastructure quirks

- **`failure_summary` is truncated to last 30 lines.** When investigating, always use the per-scenario `samples` field, not the failure_summary blob.
- **Conservative cell aggregation.** A `samples=3` cell is green only if ALL 3 passed. This makes the headline pass rate slightly pessimistic vs a "majority wins" interpretation. By design — the launch goal is consistency, not "usually works".
- **Runner re-extracts random unavail picks every run.** If you need fully reproducible runs, plumb a `--seed` flag through the runner (it currently doesn't have one).
- **`samples > 1` runs the same `_random_test_data.json` N times** — fresh phone numbers, but **same phrases**. This isolates LLM stochasticity from phrase variance. To capture phrase variance, run multiple separate `samples=1` invocations (each generates fresh phrases).

### 9.3 What this system does NOT test

- **Image extraction quality** — uses cached extractions. To test extraction, run `validate_corpus.py` on fresh images and inspect the resulting JSON.
- **WhatsApp delivery / Meta API** — the outbound `send_whatsapp_message` is monkey-patched out. Use real bot 74 (Gordão Lanches) for end-to-end Meta testing.
- **Payment flow** — checkout test stops at the delivery method prompt; it doesn't go through Mercado Pago.
- **Multi-bot isolation** — every test creates fresh bots with unique `phone_number_id`s. Cross-bot bleed-through is structurally impossible in this layer; if you need to test it, write a hook test instead.

---

## 10. Quick reference

### 10.1 Run commands

```bash
# Hook tests (Layer 1) — must be 47/47
docker compose exec backend pytest tests/simulation/ -v \
  --ignore=tests/simulation/test_multi_restaurant.py \
  --ignore=tests/simulation/test_real_restaurants.py \
  --ignore=tests/simulation/test_real_restaurants_v2.py \
  --ignore=tests/simulation/test_random_restaurants.py

# Corpus QA (Layer 2)
python tests/simulation/corpus/run_corpus_qa.py                              # random pool, samples=1
python tests/simulation/corpus/run_corpus_qa.py --pool=baseline              # baseline pool, samples=1
python tests/simulation/corpus/run_corpus_qa.py --pool=baseline --samples=3  # the launch-gate run

# Corpus pipeline
python tests/simulation/corpus/classifier_server.py        # → http://localhost:8888/classifier_game.html
python tests/simulation/corpus/validate_corpus.py          # extract products via gpt-4o
python tests/simulation/corpus/pick_baseline_pool.py       # rebuild baseline_pool.json (rare)
```

### 10.2 Files to know

| File | Role |
|---|---|
| `tests/simulation/corpus/run_corpus_qa.py` | Runner — entry point for everything |
| `tests/simulation/test_random_restaurants.py` | The 21 test classes |
| `tests/simulation/corpus/phrase_bank.py` | Phrase templates + `format_add` |
| `tests/simulation/corpus/baseline_pool.json` | Locked 10-restaurant pool |
| `tests/simulation/corpus/manifest.json` | All validated corpus entries |
| `tests/simulation/_random_test_data.json` | Per-restaurant scenario phrases for the current run (rewritten every run) |
| `app/whatsapp.py` | Pre-router guards, intent dispatch |
| `app/item_extraction.py` | `extract_items_with_quantities` + `_STOP` |
| `app/qa_reports.py` | S3 upload/list/get |
| `app/qa_lock.py` | Redis single-run lock |
| `app/monitoring_routes.py` | `/admin/corpus/*` HTTP endpoints |

### 10.3 Headline numbers in a report

```python
{
  "pool": "baseline",                       # or "random"
  "samples_per_restaurant": 3,
  "total_tests": 630,                       # 10 rests × 21 scenarios × 3 samples
  "passed": 525,
  "failed": 90,
  "skipped": 15,
  "pass_rate": 83.3,                        # incl. easy scenarios
  "comprehension_total": 540,               # excludes greeting/suggestions/checkout
  "comprehension_passed": 444,
  "comprehension_pass_rate": 82.2,          # ← LAUNCH GATE: ≥80%
  "worst_restaurant_name": "...",
  "worst_restaurant_comp_rate": 76.5,       # ← LAUNCH GATE: ≥70%
  "cross_restaurant_spread_pp": 6.1,        # ← LAUNCH GATE: ≤12pp
  "integrity": "ok",
  "restaurants": [ ... ]
}
```

### 10.4 Cleanup

```bash
# Orphan test bots from crashed runs
python -m tests.simulation.random_restaurant_runner --cleanup-only

# All Doce Café-style ephemeral test bots are matched by phone_number_id LIKE 'rand-test-%'
# Bot 74 (Gordão Lanches) is explicitly excluded from cleanup. Always.
```

---

## Appendix A — Historical fixes

| Fix | Date | What it fixed | Detection method |
|---|---|---|---|
| F1 (FINISH guard) | 2026-04-08 | `vamo finalizar` slang misclassified as ADD | Pillar 1 corpus run |
| F2 (`_QTY_REDUCE_RE`) | 2026-04-09 | `deixa só N X` was treated as SUBTRACT not SET | qty_reduction 0/10 |
| F3 (REMOVE guards) | (predates) | "tira o X" / "cancela esse X" / "não manda o X" misclassified as ADD | Various |
| F4 (`_CLEAR_KEYWORD_RE`) | 2026-04-09 | `limpa tudo` swallowed by suggestion handler | trap_clear 4/10 |
| F5 (verb-prefix bypass) | 2026-04-09 | Suggestion handler intercepted explicit ADD verbs | trap_unrelated_add 6/10 |
| Phrase bank qty-drop | 2026-04-09 | `format_add(qty=2)` could pick `{name}`-only template, drop qty silently | Doce Café cascade fail |
| `_STOP += "rola"` | 2026-04-09 | `rola` slang verb leaking into extracted item names | continuation failure |
| HF Hub graceful degradation | 2026-04-09 | Embedding model crash → entire bot crash → "algo deu errado" | Pão do Jão 14% baseline |

## Appendix B — Data flow checklist for a new corpus QA run

When something looks wrong, walk through this list to bisect the failure:

1. ✅ **Lock acquired?** Check `qa_lock.get_holder()`.
2. ✅ **Restaurants picked?** Count `len(selected)` in stdout — should be 5 (random) or 10 (baseline).
3. ✅ **Bots created?** Look for `OK (id=...)` lines per restaurant.
4. ✅ **Products loaded?** The runner logs `Creating: NAME...` per restaurant. If you see `FAIL (xxx)`, the `/bots` endpoint rejected the create.
5. ✅ **Subscriptions inserted?** Look for the SQL INSERT into `subscription`.
6. ✅ **`_random_test_data.json` written?** It's at `tests/simulation/_random_test_data.json`. Should contain one entry per bot with all 21 scenario fields populated.
7. ✅ **Pytest invocation succeeded?** Look for the `python -m pytest ...` line in stdout, then the per-test `PASSED`/`FAILED` lines.
8. ✅ **Per-sample report parsed?** Each sample produces a parsed dict; missing entries indicate the parser hit malformed pytest output.
9. ✅ **Merge succeeded?** `_merge_sample_reports` should produce `restaurants: [...]` with all the expected names.
10. ✅ **`integrity == "ok"`?** If not, one or more restaurants had zero comprehension attempts logged — check the `integrity_warning` field.
11. ✅ **Report uploaded?** Look for `Report uploaded to S3: qa-reports/...` in stdout. If S3 fails, look for `Report saved locally: ...`.
12. ✅ **Cleanup ran?** `Cleaning up...` and `Done!` at the very end. If you see neither, the runner crashed before cleanup and there are orphan bots in the DB — run `--cleanup-only`.
