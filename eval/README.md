# Evaluation

No evaluation set is bundled. `run_eval.py` reads `eval/eval_set.json`, a list of items with `id`, `user`, `question`, `must_contain`, `forbidden` and optional `expect_refusal` (see `grade()` in `run_eval.py`). Supply your own set against the demo data, then run `python run_eval.py --models claude-haiku-5-5`. Use `pytest -q` for the included automated tests, which do not call the API.
