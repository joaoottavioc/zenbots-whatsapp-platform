Run the random restaurant simulation test pipeline.

This command:
1. Picks 5 random restaurant categories (from 30+ types)
2. Searches DuckDuckGo for real São Paulo restaurants
3. Generates menus via gpt-4o-mini
4. Creates bots + products via the ZenBots API
5. Marks 2 random products unavailable per restaurant
6. Runs 35 simulation tests (7 scenarios × 5 restaurants)
7. Reports results and cleans up

Execute this exact command:

```bash
export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2) && export PYTHONIOENCODING=utf-8 && python -m tests.simulation.random_restaurant_runner --count $ARGUMENTS
```

If no argument is provided, default to 5 restaurants.

After the command finishes, summarize the results in a table showing:
- Each restaurant name and type
- Per-scenario pass/fail
- Overall score
- Any notable failures

Compare the score to the historical baseline (~80% expected).
