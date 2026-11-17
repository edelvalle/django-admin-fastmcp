# GitHub Actions Workflows

This directory contains GitHub Actions workflows for automated code quality checks and testing.

## Workflows

### `ci.yml` - Continuous Integration

**Triggers**: Pull Requests against main

**Jobs**:

- **Lint & Format Check**: Verifies code formatting and linting rules using `make lint`
- **Type Check**: Runs type checking with `make typecheck` (zuban)
- **Tests & Coverage**: Runs the test suite with a coverage report using `make coverage`, for every Django series that `pyproject.toml` supports (5.0 to 6.1), each on the oldest and the newest Python that both `pyproject.toml` and that Django series support

**Requirements**: Tests will only run if linting passes.

## Makefile Targets Used

- `make install` - Install dependencies using uv; `PYTHON_VERSION` selects the Python to pin
- `make lint` - Check formatting and linting (ruff)
- `make typecheck` - Run type checking (zuban)
- `make coverage` - Run tests with coverage reporting; `DJANGO_VERSION` (e.g. `5.1`) runs them against that Django series instead of the one in `uv.lock`

## Running Locally

```bash
make install    # Install dependencies
make lint       # Check formatting/linting
make typecheck  # Type checking
make coverage   # Tests with coverage
make coverage DJANGO_VERSION=5.1  # Tests against another Django series
```
