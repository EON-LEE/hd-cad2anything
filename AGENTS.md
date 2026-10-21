# Repository guidance for agents

## Rules

- This repository is public. Never commit customer names, real drawings, real transaction or personal data, real brand mappings, secrets, or private package-mirror URLs.
- All brand names in generated data are fictional. Real-brand mappings live only in `data/local/` (git-ignored).
- Generated data goes to `data/generated/` (git-ignored) and must be reproducible from scripts with fixed seeds. Commit only small samples under `data/samples/`.
- Geometry uses a local engineering CRS in meters (SRID 0) with the building's south-west corner as origin, shared by all floors. Do not use map services.
- Deterministic code computes geometry, areas, adjacency, diffs, scores, and layout search. LLMs only select tools, interpret semantics, and explain results.
- Contracts: `src/cad2anything/model.py` (data model), `src/cad2anything/catalog.py` (domain catalog), `docs/specs/cad-conventions.md`, and `docs/specs/data-layout.md`. Change them deliberately and update dependent modules and tests together.
- Human-facing documents are written in Korean. Code identifiers and code comments are in English.

## Commands

```powershell
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

If direct PyPI access is blocked, set `UV_DEFAULT_INDEX` to an approved mirror for the current process only and do not commit `uv.lock`.
