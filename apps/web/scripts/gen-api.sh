#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd -P)"

cd -- "$REPO_ROOT"
# Input/output paths come from the `apis.goalx@v1` entry in redocly.yaml.
exec bun x openapi-typescript
