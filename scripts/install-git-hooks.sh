#!/bin/sh
# Use the repo's tracked hooks (scripts/git-hooks/) for this clone.
set -e
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath scripts/git-hooks
echo "git hooks enabled: core.hooksPath=scripts/git-hooks"
[ -f config/dev.yaml ] || echo "next: cp config/dev.yaml.example config/dev.yaml and adjust precommit_privacy_check"
