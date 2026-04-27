#!/usr/bin/env bash
# Invoked by `experimental.scripts.postdeploy` in databricks.yml.
# Runs after `databricks bundle deploy`; grants USE_CATALOG / USE_SCHEMA on the
# parents of every uc_securable declared under every app in the bundle.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
bundle_dir="${BUNDLE_ROOT:-$(pwd)}"

# Prefer the repo's venv if it exists; fall back to $PYTHON or python3.
# On PEP 668 systems (recent macOS / Debian), --user installs are blocked, so
# we retry with --break-system-packages as a last resort. For CI, pre-create
# a .venv at the bundle root to skip pip altogether.
if [[ -x "$repo_root/.venv/bin/python" ]]; then
    python_bin="$repo_root/.venv/bin/python"
else
    python_bin="${PYTHON:-python3}"
    if ! "$python_bin" -m pip install --quiet --disable-pip-version-check \
            --user 'databricks-sdk>=0.30.0' >/dev/null 2>&1; then
        "$python_bin" -m pip install --quiet --disable-pip-version-check \
            --user --break-system-packages 'databricks-sdk>=0.30.0' >/dev/null
    fi
fi

"$python_bin" "$here/grant_app_parents.py" \
    ${DATABRICKS_CONFIG_PROFILE:+--profile "$DATABRICKS_CONFIG_PROFILE"} \
    bundle \
    --bundle-dir "$bundle_dir" \
    ${DATABRICKS_BUNDLE_TARGET:+--target "$DATABRICKS_BUNDLE_TARGET"}
