# GitHub Actions Workflows

This directory contains GitHub Actions workflows for automated code quality checks and testing.

## Workflows

### `ci.yml` - Continuous Integration

**Triggers**: Pull Requests against main

**Jobs**:

- **Lint & Format Check**: Verifies code formatting and linting rules using `make lint`
- **Type Check**: Runs type checking with `make typecheck` (zuban)
- **Tests & Coverage**: Runs the test suite with a coverage report using `make coverage`, on Python 3.11 and 3.14 (the oldest and the newest Python that `pyproject.toml` supports)

**Requirements**: Tests will only run if linting passes.

## Makefile Targets Used

- `make install` - Install dependencies using uv; `PYTHON_VERSION` selects the Python to pin
- `make lint` - Check formatting and linting (ruff)
- `make typecheck` - Run type checking (zuban)
- `make coverage` - Run tests with coverage reporting

## Running Locally

```bash
make install    # Install dependencies
make lint       # Check formatting/linting
make typecheck  # Type checking
make coverage   # Tests with coverage
```
