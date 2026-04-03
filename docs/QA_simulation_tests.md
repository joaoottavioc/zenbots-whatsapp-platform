# QA Simulation Tests — Architecture & Operations Guide

> **Last updated:** 2026-04-03
> **Current scores:** Baseline 47/47 (100%), Corpus-based ~89%, Real restaurants ~83%

---

## Overview

ZenBots has a three-layer simulation test system that validates the conversational ordering engine against diverse restaurant menus. Each layer serves a different purpose:

```
┌─────────────────────────────────┐
│  Corpus QA (real menu images)   │  Exploratory: real menus from the web
│  /test-restaurants              │  extracted by Cadastro Mágico (gpt-4o)
│  ~89% pass rate                 │  Trigger: on-demand or monthly
└────────────────┬────────────────┘
                 │
┌────────────────┴────────────────┐
│  Hook Tests (Sabor da Serra)    │  Regression: must always be 100%
│  47 deterministic tests         │  Trigger: auto on file edit (PostToolUse hook)
│  100% pass rate                 │  + every PR via CI
└─────────────────────────────────┘
```

---

## Layer 1: Sabor da Serra Baseline (47 tests)

### What it is

A fictional lanchonete ("Sabor da Serra") with 24 hand-crafted products designed to stress-test specific patterns: similar names (Mignon vs Mignon ao molho cheddar), compound names (Picanha com bacon), typo-prone names, unavailable products, and short names (Big Mix, Hot Dog, Fit Wrap).

### Files

```
tests/simulation/
├── conftest.py                      # Session-scoped fixtures: creates bot + products + embeddings
├── menu.py                          # 24 products across 6 categories
├── test_add_items.py                # 5 tests: single, multi, typos, compound numbers, hyphenated
├── test_remove_items.py             # 5 tests: digit, written, compound, multi-item, full removal
├── test_unavailable_items.py        # 5 tests: available + unavailable mixes
├── test_request_suggestions.py      # 5 tests: informal, indecisive, popular, first-timer, dicas
├── test_suggestion_selection.py     # 9 tests: bare number, ordinal, name, qty+digit, compound
├── test_suggestion_to_order.py      # 5 tests: manda ver, unavailable, digits, indecisive→order
├── test_short_name_products.py      # 5 tests: Big Mix, Hot Dog, Fit Wrap, differentiation
└── test_checkout_protection.py      # 8 tests: ambiguous messages during 6 checkout states
```

### How to run

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

### When it runs automatically

A PostToolUse hook triggers these tests when critical files are edited:
`whatsapp.py`, `item_extraction.py`, `semantic_router.py`, `prompt_central.py`

### Expected result

**47/47 (100%).** Any failure here means a regression in the core ordering engine.

---

## Layer 2: Corpus-Based QA (real menu images)

### What it is

An automated pipeline that tests the ordering engine against real restaurant menu images collected from the web. Each run:

1. Picks 5 random images from the corpus (different categories)
2. Creates bots with products from cached Cadastro Mágico extractions
3. Marks 2 random products as unavailable
4. Runs 7 test scenarios per restaurant
5. Cleans up all test data from DB

### The 7 Scenarios

| # | Scenario | What it tests | Example message |
|---|---|---|---|
| 1 | **Add single** | Basic product matching | "quero um filé de frango" |
| 2 | **Add multi** | Quantity parsing + multi-item | "quero 2 margherita e 1 coca-cola" |
| 3 | **Unavailable** | Em falta detection | "quero um X (unavail) e um Y (avail)" |
| 4 | **Remove** | Cart item removal | "tira o filé de frango" |
| 5 | **Suggestions** | Suggestions don't add to cart | "o que tem de bom?" |
| 6 | **Checkout** | Ambiguous msg doesn't break checkout | "hmm deixa eu pensar" during CEP entry |
| 7 | **Greeting** | Greeting doesn't add to cart | "oi, boa noite" |

### Architecture

```
tests/simulation/corpus/
├── images/                          # 30+ real menu photos (jpg/png)
│   ├── menu_0001.jpg
│   └── ...
├── manifest.json                    # Index: id, category, product_count, validated
├── extractions/                     # Cached product data per image
│   ├── menu_0001.json               # {products: [...], category: "pizzaria"}
│   └── ...
├── rejected/                        # Images that failed validation (<5 products)
├── pending_images.json              # ~2000 URLs from DuckDuckGo (for the game)
├── classifier_game.html             # Browser game for accepting/rejecting images
├── classifier_server.py             # Backend for the game (downloads on accept)
├── validate_corpus.py               # Validates images via Cadastro Mágico
└── run_corpus_qa.py                 # Runs QA tests against validated corpus
```

### How to run

```bash
# Quick: run 5 random restaurants from corpus
python tests/simulation/corpus/run_corpus_qa.py

# Or via Claude Code command:
/test-restaurants 5
```

### Expected result

**~85-90%.** Known weaknesses: abbreviated single-word products in add_multi (~50%), unavailable detection (~75%). See "Known Failure Modes" below.

---

## Building the Corpus

### Step 1: Fetch Image URLs (one-time or monthly refresh)

Playwright scrapes DuckDuckGo Images in headed mode for 20 menu-related queries. Produces `pending_images.json` with ~2,000 image URLs.

```bash
# This was already run. To refresh:
# Edit the queries in the fetch script and re-run.
# The pending_images.json in the repo has 1,932 URLs.
```

### Step 2: Classify Images (human-in-the-loop game)

```bash
# Start the game server
python tests/simulation/corpus/classifier_server.py

# Open in browser
# http://localhost:8888/classifier_game.html

# Controls:
#   → or Enter = accept (downloads image to corpus/images/)
#   ← or S     = skip
#   Z           = undo
#
# Each level = 30 accepted images
# Failed downloads silently skip to next
# Progress auto-saves to localStorage
```

### Step 3: Validate via Cadastro Mágico

After collecting images, validate them through the extraction pipeline:

```bash
# Requires: Docker services running + OPENAI_API_KEY in .env
python tests/simulation/corpus/validate_corpus.py
```

This uploads each image to `POST /bots/{id}/catalog/upload-from-file`, extracts products via gpt-4o, and auto-detects the restaurant category. Images with <5 products are rejected and moved to `corpus/rejected/`.

**Cost:** ~$0.03 per image (one gpt-4o vision call). 30 images = ~$0.90.

### Step 4: Run Tests

```bash
python tests/simulation/corpus/run_corpus_qa.py
```

---

## DB Cleaning

Test bots are ephemeral. The system cleans up in three layers:

### Layer 1: Pre-run cleanup

Before every run, delete ALL orphan test bots from previous crashed runs:

```sql
DELETE FROM bot WHERE phone_number_id LIKE 'rand-test-%'
  OR phone_number_id LIKE 'corpus-build-%'
  -- Cascades through: contacts, carts, cart_items, orders, order_items,
  --   conversation_history, products, subscriptions
  -- NEVER deletes bot 74 (Gordão Lanches)
```

### Layer 2: Post-run cleanup (in finally block)

After tests complete (or crash), delete bots created in this specific run. Runs even on KeyboardInterrupt or exceptions.

### Layer 3: Safety guards

- All test bots use predictable `phone_number_id` patterns: `rand-test-*`, `real-test-*`, `corpus-build-*`
- Cleanup SQL always filters on these patterns
- Bot 74 (Gordão Lanches) is explicitly excluded
- Refuse to run if `ENVIRONMENT=production`

### Manual cleanup

```bash
# If the DB has orphan test bots:
python -m tests.simulation.random_restaurant_runner --cleanup-only
```

---

## Known Failure Modes

Ordered by frequency across 15+ real restaurant tests:

| # | Failure | Pass Rate | Root Cause | Status |
|---|---|---|---|---|
| F7 | Unavailable detection | ~75% | Single-word items filtered by `_real_pairs` before em falta check | Partially fixed (2026-04-03) |
| F8 | Abbreviated names | ~60% | "Brahma" doesn't match "Cerveja Brahma 600ml" | Open |
| F9 | Complement filtering | ~80% | "Granola" filtered by Option C category exclusion | Open |
| F10 | Similar name confusion | ~85% | "Tradicional" vs "Nutricional" | Open |
| F1 | Remove defaults to qty=1 | 100% | Was removing 1 instead of all | **Fixed** |
| F3 | Size confusion | 100% | 300ml vs 500ml identical after normalization | **Fixed** |
| F4 | Number-word as qty | 100% | "2 quatro queijos" → qty=8 | **Fixed** |

### What all failures trace back to

The LLM doesn't see the full menu. Products are found via RAG search, and the LLM only sees search results. When RAG doesn't find a product (unavailable, abbreviated, unusual name), the LLM can't handle it. The fix for most remaining failures is the **full menu context plan** — pass all products in the LLM prompt.

---

## Historical Results

| Date | Source | Restaurants | Score | Notes |
|---|---|---|---|---|
| 2026-04-02 | V1 real (manual) | 5 (Nakato, Bullguer, Black Dog, Macedos, Tubarão) | 29/35 (83%) | Before fixes |
| 2026-04-02 | V2 real (manual) | 5 (Yokoyama, Marmitaria, Santiago, Tapilícia, Espetinhos) | 28/35 (80%) | Before fixes |
| 2026-04-02 | V1 real (after fixes) | 5 | 29/35 (83%) | Remove fixed, size fixed |
| 2026-04-02 | Random (LLM menus) | 5 | 32/35 (91%) | Inflated — LLM grades own homework |
| 2026-04-03 | Corpus (real images) | 4 | 25/28 (89%) | First honest corpus run |

---

## File Reference

### Core test files (don't modify unless fixing bugs)

| File | Purpose |
|---|---|
| `tests/simulation/conftest.py` | Sabor da Serra session fixtures |
| `tests/simulation/menu.py` | Sabor da Serra menu definition |
| `tests/simulation/test_*.py` (9 files) | 47 baseline tests |

### Corpus infrastructure

| File | Purpose |
|---|---|
| `tests/simulation/corpus/classifier_game.html` | Browser game for image classification |
| `tests/simulation/corpus/classifier_server.py` | Game backend (downloads images) |
| `tests/simulation/corpus/validate_corpus.py` | Validates images via Cadastro Mágico |
| `tests/simulation/corpus/run_corpus_qa.py` | Runs QA tests against corpus |
| `tests/simulation/corpus/pending_images.json` | ~2000 image URLs from DuckDuckGo |
| `tests/simulation/corpus/manifest.json` | Corpus index (validated images) |
| `tests/simulation/corpus/images/` | Downloaded menu images |
| `tests/simulation/corpus/extractions/` | Cached product extractions |

### Runner infrastructure

| File | Purpose |
|---|---|
| `tests/simulation/random_restaurant_runner.py` | Original runner (web search + LLM fallback) |
| `tests/simulation/test_random_restaurants.py` | Dynamic test file (reads `_random_test_data.json`) |
| `.claude/commands/test-restaurants.md` | Claude Code `/test-restaurants` command |

### Reports

| Location | Purpose |
|---|---|
| `.claude/simulation-reports/runs/*.json` | Structured JSON per run (for dashboard) |
| `.claude/simulation-reports/random_run_*.md` | Markdown reports |
| `.claude/simulation-reports/real_simulations_results.md` | Initial real restaurant report |

---

## Quick Reference

```bash
# Run baseline (must be 100%)
docker compose exec backend pytest tests/simulation/ -v --ignore=tests/simulation/test_multi_restaurant.py --ignore=tests/simulation/test_real_restaurants.py --ignore=tests/simulation/test_real_restaurants_v2.py --ignore=tests/simulation/test_random_restaurants.py

# Run corpus QA (target: 85%+)
python tests/simulation/corpus/run_corpus_qa.py

# Collect more menu images
python tests/simulation/corpus/classifier_server.py
# → open http://localhost:8888/classifier_game.html

# Validate new images
python tests/simulation/corpus/validate_corpus.py

# Clean up orphan test bots
python -m tests.simulation.random_restaurant_runner --cleanup-only
```
