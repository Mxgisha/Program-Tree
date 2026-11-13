# Demo fixture

A tiny application used by tests and documentation screenshots.

It intentionally contains dead code so the analyzer has known ground truth:

- `app/cli.py` is never imported by the app;
- `app/util.py` has an unused helper, an unused public function, and a
  callable object used only by that dead function.
