# Deckard rebrand — Phase 4 runbook (remotes + local checkout)

Run **only after MR !61 is merged to `main`**. Each host keeps an old→new
redirect, so every rename here is reversible (see Rollback). A guarded script
does steps 1–6: `scripts/phase4-rename.sh` (dry-run by default; `--go` to
execute). Step 7 (local dir) is manual.

Confirmed facts (2026-07-14): GitLab project **id 15** = `naz/StreamController`
(glab authed as `naz`); GitHub fork `nazbert/StreamController` (gh authed as
`nazbert`, isFork=true). Current remotes: `fork`→github nazbert, `gitlab`→
nb-labs naz, `origin`→ upstream StreamController/StreamController.

## Precondition

```sh
git fetch gitlab && git log --oneline gitlab/main | grep -q 'rebrand fork to Deckard' \
  && echo "OK: rename is on gitlab/main" || echo "STOP: MR !61 not merged yet"
```

## 1. Rename the GitLab project (naz/StreamController → naz/deckard)

`name` = display name, `path` = URL slug. GitLab redirects the old path.

```sh
glab api --method PUT projects/15 -f path=deckard -f name=Deckard
glab api projects/15 --jq '.path_with_namespace'   # expect: naz/deckard
```

## 2. Repoint the local `gitlab` remote

```sh
git remote set-url gitlab https://gitlab.nb-labs.net/naz/deckard.git
git ls-remote gitlab HEAD >/dev/null && echo "gitlab reachable"
```

## 3. Rename the GitHub fork (nazbert/StreamController → nazbert/deckard)

Stays a fork of `StreamController/StreamController`; GitHub redirects the old URL.

```sh
gh repo rename deckard -R nazbert/StreamController --yes
gh repo view nazbert/deckard --json nameWithOwner --jq '.nameWithOwner'   # expect: nazbert/deckard
```

## 4. Repoint the local `fork` remote

```sh
git remote set-url fork git@github.com:nazbert/deckard.git
git ls-remote fork HEAD >/dev/null && echo "fork reachable"
```

## 5. (Optional) Clarify the upstream remote name

`origin` points at the real upstream now, not our fork — renaming the **remote**
(local only, not the repo) removes the ambiguity:

```sh
git remote rename origin upstream
```

## 6. Verify

```sh
git remote -v
```

Expect: `fork` → `git@github.com:nazbert/deckard.git`, `gitlab` →
`https://gitlab.nb-labs.net/naz/deckard.git`, `upstream` (or `origin`) →
`https://github.com/StreamController/StreamController.git`.

## 7. Rename the local checkout — LAST, manual

```sh
cd ~ && mv ~/dev/StreamController ~/dev/Deckard && cd ~/dev/Deckard
```

Breaks this session's cwd and touches things keyed to the old path:

- **Claude Code memory** is keyed to the cwd (`~/.claude/projects/-home-naz-dev-StreamController/`). Move it so history follows (MEMORY.md links are relative, so it's safe):
  ```sh
  mv ~/.claude/projects/-home-naz-dev-StreamController ~/.claude/projects/-home-naz-dev-Deckard
  ```
- **`Deckard` wrapper symlink** (if installed) points at the old checkout — recreate it:
  ```sh
  ln -sf ~/dev/Deckard/scripts/Deckard ~/.local/bin/Deckard
  ```
- **Installed desktop/autostart entries** embed the absolute old `.venv`/`main.py` paths. They regenerate on the next app launch (`ensure_app_desktop_entry` / `setup_autostart` run every start), so just launch Deckard once from the new path.
- Reopen your editor/terminal/Claude session from `~/dev/Deckard`.

## Follow-ups (not part of Phase 4)

- `ci/flatpak-releases` (#128) manifest filename/app-id — adopt before that branch commits.
- About/README/install.sh/metainfo URLs now point directly at the renamed repos (naz/Deckard, nazbert/Deckard).

## Rollback

Both hosts keep old→new redirects, so rename back and reset URLs:

```sh
glab api --method PUT projects/15 -f path=StreamController -f name=StreamController
gh repo rename StreamController -R nazbert/deckard --yes
git remote set-url gitlab https://gitlab.nb-labs.net/naz/StreamController.git
git remote set-url fork  git@github.com:nazbert/StreamController.git
# and `git remote rename upstream origin` if step 5 was applied
```
