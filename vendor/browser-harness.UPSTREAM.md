# browser-harness upstream

Vendored from: https://github.com/browser-use/browser-harness

Pinned commit: `f186cd963d67653a737bfaaf43e0edbfe85ddb77`

Update process:

1. Review upstream changes and compatibility with this bot's generated harness scripts.
2. Replace `vendor/browser-harness` with the chosen upstream tree, excluding its `.git` directory.
3. Update the pinned commit above.
4. Run `pytest tests/unit/test_browser_setup.py -q`.
