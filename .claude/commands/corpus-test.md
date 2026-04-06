Run QA simulation tests against the validated corpus of real restaurant menu images.

This picks 5 random images from different categories, creates bots with cached product data, marks 2 random products as unavailable, runs 7 test scenarios per restaurant (35 total), and cleans up all test data.

**Requires:** Docker services running (backend + db + worker) and at least 5 validated corpus images.

Execute this exact command:

```bash
export PYTHONIOENCODING=utf-8 && python tests/simulation/corpus/run_corpus_qa.py
```

After the command finishes, summarize the results in a table showing:
- Each restaurant name and category
- Per-scenario pass/fail (add_single, add_multi, unavailable, remove, suggestions, checkout, greeting)
- Overall score (e.g., 30/35)
- Any notable failures and which failure mode they match (F7, F8, F9, F10)

Compare the score to the historical baseline (~89% expected).
