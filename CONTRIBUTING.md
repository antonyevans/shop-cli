# Contributing

`shop` is alpha software. Contributions are welcome, but expect the API to shift until v1.0.

## Status

See the alpha banner in the README — core flows work, PyPI release pending. Check open issues before starting large changes.

## Running tests

```bash
pip install -e ".[dev]"
pytest tests/             # unit tests (fast, no network)
pytest tests/integration/ # integration tests (require credentials — see .env.integration.example)
```

CI requires 80% coverage. Lint with `ruff check src/ tests/`.

## Filing issues

Open a GitHub issue. Include:
- `shop --version` output
- The command you ran
- The JSON output or error you got
- Expected behavior

## Pull requests

- Keep PRs focused — one thing per PR
- All commands must return JSON on stdout, including errors
- New commands need unit tests; new adapters need integration test stubs
- Exit codes are part of the public API — don't change them without a version bump
