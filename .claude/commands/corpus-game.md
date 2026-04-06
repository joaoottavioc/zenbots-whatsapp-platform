Start the menu classifier game to collect new corpus images.

This launches a local server at http://localhost:8888 where you classify menu images from DuckDuckGo results. Accepted images are downloaded to `tests/simulation/corpus/images/`.

Execute this exact command:

```bash
python tests/simulation/corpus/classifier_server.py
```

After launching, tell the user:

1. Open **http://localhost:8888/classifier_game.html** in your browser
2. Controls:
   - **→ or Enter** = accept image (downloads it)
   - **← or S** = skip
   - **Z** = undo last action
3. Each level = 30 accepted images
4. Progress auto-saves to localStorage
5. Close the tab and Ctrl+C the server when done

Then tell them to run `/corpus-validate` to extract products from the new images.
