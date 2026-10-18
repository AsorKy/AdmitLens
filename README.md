# AdmitLens

An educational admissions CRM for behavioral-health facilities, built **four times at increasing levels of complexity** — from a junior monolith to a cloud deployment — inside one repository. All data in this repository is synthetic.

> [!NOTE]
> Work in progress. Level 1 is being built first; the other levels follow once the previous one is operational.

## Levels

| Folder | Level | Architecture |
|---|---|---|
| `level-1/` | Junior | Synchronous FastAPI monolith with a pure domain, on PostgreSQL 16 |
| `level-2/` | Mid | Async modular monolith: multi-tenant, Celery workers, Redis, cached reports |
| `level-3/` | Mid+ | Services behind a gateway, events through an outbox/inbox over Redis Streams |
| `level-4/` | Cloud | Level 3 deployed with a CI/CD pipeline that tests, deploys, verifies and rolls back |

Each level is self-contained (`level-N/backend` and `level-N/frontend`) and never imports another level. Compare two levels side by side with:

```bash
git diff --no-index level-1 level-2
```

## Getting started

Requirements: `git`, GNU Make 3.81 or newer, Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
make setup    # installs the git hooks and the repository's local git safety settings
make doctor   # checks hooks, commit identity and tool versions
make help     # lists every available target
```

Every operation — starting services, running tests, scanning for secrets — runs through `make`, both locally and in CI. Each level has its own `Makefile`; run `make help` inside `level-N/backend` too.

## Data policy

- **No data is ever committed.** `data/` is git-ignored; datasets are generated locally.
- The synthetic data is calibrated with public SAMHSA datasets (TEDS-D and N-SUMHSS), which are used locally only and are not redistributed here.
- Names in code, docs and seeds are generic; there is no real patient, facility or company data.

## Security

Leaks are prevented in layers: a strict `.gitignore`, git hooks that scan every commit and push, project-specific detection rules with their own tests, CI scans, and GitHub secret scanning with push protection.

To report a vulnerability, follow [SECURITY.md](SECURITY.md) — please do not open a public issue.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
