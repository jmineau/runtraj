# runtraj development tasks. CI runs these same recipes.

set positional-arguments

# Unit tests only: the integration tests need met files and HYSPLIT, or its recorded runs (AGENTS.md, Tests).
unit := "not integration"

# Show available recipes
list:
    @just --list

# Install the project and dev tools into .venv
sync:
    uv sync

# Update uv.lock after changing dependencies
lock:
    uv lock

# Lint and check formatting (no changes)
lint:
    uv run ruff check
    uv run ruff format --check

# Fix lint and format the code
format:
    uv run ruff check --fix
    uv run ruff format

# Type check with pyrefly
type-check:
    uv run pyrefly check

# Check the import contracts in pyproject.toml (the package's layers)
imports:
    uv run lint-imports

# Require docstrings on the public API
docstr:
    uv run docstr-coverage src/runtraj --skip-magic --skip-init --fail-under 95

# Run the unit tests in parallel (up to 8 workers; `-n 0` for serial)
test *args:
    uv run pytest -n auto --maxprocesses=8 -m "{{ unit }}" "$@"

# The integration tests on the runs recorded on HYSPLIT VERSION (tests/recorded/hysplit/): no HYSPLIT needed, only the test met files
test-replay version *args:
    uv run pytest tests -m integration -n auto --maxprocesses=8 --dist loadgroup --hysplit-replay "$@"

# Make a HYSPLIT folder at DEST that replays the runs recorded on VERSION, and print its exec/ (put it first on PATH)
replay-folder version dest:
    @uv run python tests/fixtures/replay.py folder "$2" --cases "tests/recorded/hysplit/$1"

# The integration tests on each real HYSPLIT build in hysplit-builds.toml, or on the VERSIONS given
test-real *versions:
    #!/usr/bin/env bash
    set -euo pipefail
    builds="$(uv run python tests/fixtures/replay.py builds "$@")"
    while read -r version build <&3; do
        echo "== HYSPLIT $version: $build"
        PATH="$build/exec:$PATH" uv run pytest tests -m integration -n auto --maxprocesses=8 --dist loadgroup
    done 3<<< "$builds"

# Record the integration tests' runs on each build in hysplit-builds.toml (or the VERSIONS given), replacing tests/recorded/hysplit/<version>/
record *versions:
    #!/usr/bin/env bash
    set -euo pipefail
    builds="$(uv run python tests/fixtures/replay.py builds "$@")"
    met="${RUNTRAJ_TEST_MET_DIR:-tests/met_cache}"
    while read -r version build <&3; do
        echo "== recording HYSPLIT $version: $build"
        cases="tests/recorded/hysplit/$version"
        rm -rf "$cases"
        uv run pytest tests -m integration -n auto --maxprocesses=8 --dist loadgroup \
            --hysplit-record "$build" --hysplit-cases "$cases"
        uv run python tests/fixtures/replay.py summary "$cases" --record "$build" --met "$met"
    done 3<<< "$builds"

# Record the HYSPLIT runs of the Kubernetes test (tests/kubernetes/demo) on the build of VERSION, replacing tests/recorded/kubernetes/<version>/
record-kubernetes version="5.1.0":
    #!/usr/bin/env bash
    set -euo pipefail
    read -r _ build <<< "$(uv run python tests/fixtures/replay.py builds "$1")"
    met="$(realpath "${RUNTRAJ_TEST_MET_DIR:-tests/met_cache}")"
    cases="tests/recorded/kubernetes/$1"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    # The demo with the Kubernetes parts left out: the same runs, here.
    mkdir "$work/met" "$work/project"
    ln -s "$met"/20210115_00-05_* "$work/met/"
    cp tests/kubernetes/demo/releases.csv "$work/project/"
    uv run python tests/fixtures/replay.py folder "$work/hysplit" --cases "$cases" --record "$build" > /dev/null
    sed -e '/^execution:/,$d' \
        -e "s|^  hrrr: /met\$|  hrrr: $work/met|" \
        -e "s|directory: /opt/hysplit|directory: $work/hysplit|" \
        -e "s|^output: .*|output: $work/output|" \
        tests/kubernetes/demo/config.yaml > "$work/project/config.yaml"
    rm -rf "$cases"
    RUNTRAJ_COMPUTE_ROOT="$work/compute" uv run runtraj run "$work/project"
    uv run python tests/fixtures/replay.py summary "$cases" --record "$build" --met "$met"

# Install every dependency, the dev tools too, at the minimum pyproject.toml declares (the CI floors job; run `just test` next). EXTRAS: the extras to include; `just sync-floors ""` for none
sync-floors extras="--all-extras":
    #!/usr/bin/env bash
    set -euo pipefail
    uv venv --clear
    floors="$(mktemp)"
    uv pip compile pyproject.toml --group dev $1 --resolution lowest-direct --quiet -o "$floors"
    uv pip install --requirement "$floors"
    uv pip install --no-deps --editable .
    rm -f "$floors"

# Run the unit tests with coverage (coverage.xml, junit.xml for Codecov)
cov *args:
    uv run pytest -n auto --maxprocesses=8 -m "{{ unit }}" --cov --cov-report=term --cov-report=xml --junitxml=junit.xml -o junit_family=legacy "$@"

# Build the HTML docs, running every example, failing on warnings
build-docs:
    rm -rf docs/_build docs/reference/_api
    MPLCONFIGDIR="${TMPDIR:-/tmp}/runtraj-mplconfig" uv run sphinx-build -M html docs docs/_build -W --keep-going

# Serve the docs at http://127.0.0.1:PORT, rebuilding on every save (Ctrl-C stops)
docs-serve port="8000":
    uv run sphinx-autobuild docs docs/_build/html --port "$1" --watch src --re-ignore 'reference/_api/' --re-ignore '_build/' --re-ignore '__pycache__/'

# Everything the Code Quality workflow checks, plus the unit tests
quality-check: lint type-check imports docstr test

# Run every pre-commit hook on every file
pre-commit:
    uv run pre-commit run --all-files

# Build the sdist and wheel into dist/ and check them
dist:
    rm -rf dist
    uv build
    uv run twine check --strict dist/*

# At release, draft the CHANGELOG entries from the commits since the last release
changelog:
    @uv run git-cliff --unreleased --strip all

# Print the version setuptools-scm computes from git
version:
    @uv run python -m setuptools_scm

# Tag and push release VERSION (e.g. `just release 0.1.0a23`); CI publishes it
release version:
    #!/usr/bin/env bash
    set -euo pipefail
    v="$1"
    test -z "$(git status --porcelain)" || { echo "Working tree is not clean." >&2; exit 1; }
    test "$(git branch --show-current)" = main || { echo "Release from main." >&2; exit 1; }
    git fetch --quiet --tags origin main
    test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" || { echo "main is not in sync with origin/main." >&2; exit 1; }
    grep -q "^## \[$v\]" CHANGELOG.md || { echo "CHANGELOG.md has no '## [$v]' section." >&2; exit 1; }
    # PEP 440: the version must be in normal form and newer than every v* tag,
    # or installers would not see it as the latest release.
    uv run --no-sync python - "$v" <<'PY'
    import subprocess
    import sys

    from packaging.version import InvalidVersion, Version

    new = Version(sys.argv[1])
    if str(new) != sys.argv[1]:
        sys.exit(f"{sys.argv[1]} normalizes to {new}; release it as {new}.")
    tags = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True, text=True, check=True).stdout.split()
    old = []
    for tag in tags:
        try:
            old.append(Version(tag[1:]))
        except InvalidVersion:
            pass
    if old and new <= max(old):
        sys.exit(f"{new} is not newer than the latest release, v{max(old)}.")
    PY
    git tag --annotate "v$v" --message "runtraj $v"
    git push origin "v$v"
    echo "Pushed v$v; the Publish workflow builds and releases it."

# Remove build artifacts and caches
clean:
    rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache .pyrefly_cache .import_linter_cache
    rm -rf .coverage coverage.xml junit.xml htmlcov docs/_build docs/reference/_api
    find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +
