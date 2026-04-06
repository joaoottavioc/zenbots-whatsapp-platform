Validate new corpus images via Cadastro Mágico (gpt-4o vision extraction).

This uploads each unvalidated image in the corpus to the API, extracts products via gpt-4o, auto-detects the restaurant category, and saves the extraction cache. Images with <5 products are rejected.

**Requires:** Docker services running (backend + db) and OPENAI_API_KEY in .env.
**Cost:** ~$0.03 per image.

Execute this exact command:

```bash
export PYTHONIOENCODING=utf-8 && python tests/simulation/corpus/validate_corpus.py
```

After the command finishes, summarize:
- How many images were validated vs rejected
- Total corpus size
- Category breakdown

Then tell the user to run `/corpus-test` to run QA tests against the corpus.
