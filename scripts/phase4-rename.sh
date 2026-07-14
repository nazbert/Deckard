#!/usr/bin/env bash
#
# Phase 4 of the Deckard rebrand: rename the GitLab project and GitHub fork,
# then repoint the local remotes. See docs/rename-deckard-phase4.md.
#
# Run ONLY after MR !61 is merged to main. Dry-run by default; pass --go to
# execute. Does NOT rename the local checkout dir (step 7 -- do that by hand).
#
# Confirmed identifiers: GitLab project id 15 (naz/StreamController),
# GitHub fork nazbert/StreamController.
set -euo pipefail

GO=0
[[ "${1:-}" == "--go" ]] && GO=1
mode=$([[ $GO -eq 1 ]] && echo EXECUTE || echo "DRY-RUN (pass --go to execute)")

run() { printf '+ %s\n' "$*"; [[ $GO -eq 1 ]] && "$@"; return 0; }

echo "== Deckard Phase 4 rename -- $mode =="

# Precondition: the rename must already be on gitlab/main.
if [[ $GO -eq 1 ]]; then
  git fetch gitlab --quiet
  # Native message search: no pipe, so `set -o pipefail` + grep -q's SIGPIPE
  # to git log cannot produce a false-negative precondition.
  if [ -z "$(git log --grep='rebrand fork to Deckard' --format=%H gitlab/main)" ]; then
    echo "STOP: 'rebrand fork to Deckard' commit not found on gitlab/main -- merge MR !61 first." >&2
    exit 1
  fi
  echo "precondition OK: rename commit is on gitlab/main"
fi

# 1. GitLab project rename (naz/StreamController -> naz/deckard; redirect kept)
run glab api --method PUT projects/15 -f path=deckard -f name=Deckard
# glab api has no --jq; parse the path out of the JSON instead.
[[ $GO -eq 1 ]] && echo "  gitlab now: $(glab api projects/15 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["path_with_namespace"])')"

# 2. repoint local gitlab remote
run git remote set-url gitlab https://gitlab.nb-labs.net/naz/deckard.git

# 3. GitHub fork rename (nazbert/StreamController -> nazbert/deckard; redirect kept)
run gh repo rename deckard -R nazbert/StreamController --yes

# 4. repoint local fork remote
run git remote set-url fork git@github.com:nazbert/deckard.git

# 5. (optional) clarify the upstream remote name -- uncomment to apply
# run git remote rename origin upstream

# 6. verify
if [[ $GO -eq 1 ]]; then
  echo "== remotes =="
  git remote -v
  git ls-remote gitlab HEAD >/dev/null && echo "  gitlab reachable"
  git ls-remote fork   HEAD >/dev/null && echo "  fork reachable"
fi

echo "Done. Step 7 (rename ~/dev/StreamController -> ~/dev/deckard) is manual;"
echo "see docs/rename-deckard-phase4.md."
