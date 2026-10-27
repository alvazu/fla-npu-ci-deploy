#!/usr/bin/env bash
set -euo pipefail

tool_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${FLA_CI_PYTHON:-python3}" "$tool_dir/scripts/test_ci.py" "$@"
