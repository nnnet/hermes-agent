#!/usr/bin/env bash
# One-time per-clone setup. Registers the `ours` merge driver referenced by
# .gitattributes so that paths marked `merge=ours` keep our working-tree
# version during upstream merges (no conflict, no upstream edit applied).
#
# Run once after `git clone`:  scripts/setup-merge-drivers.sh
set -euo pipefail

git config merge.ours.driver true
echo "merge.ours.driver registered (returns 0 → keep our version on merge)."
