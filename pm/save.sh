#!/usr/bin/env bash
# Сохраняет pm/paper в ветку pm-results (только данные, без кода). Используется в конце прогона и каждые 45 минут.
set -u
git config --global user.name github-actions; git config --global user.email actions@github.com
for i in 1 2 3; do
  rm -rf /tmp/res; git worktree prune
  if git fetch -q origin pm-results; then git worktree add -q --detach /tmp/res FETCH_HEAD
  else git worktree add -q --detach /tmp/res && (cd /tmp/res && git checkout -q --orphan r && git rm -rqf .); fi
  mkdir -p /tmp/res/pm && cp -r pm/paper /tmp/res/pm/
  (cd /tmp/res && git add -A pm/paper && (git commit -q -m "pm-results $(date -u +%F-%H%M) ${1:-} [skip ci]" || true) && git push -q origin HEAD:pm-results) && { git worktree remove --force /tmp/res; exit 0; }
  echo "push не прошёл, попытка $i"; git worktree remove --force /tmp/res 2>/dev/null; sleep 10
done
