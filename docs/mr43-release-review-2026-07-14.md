# MR !43 review — initial release + GitHub mirror

Combined MR "v0.1.0: native packaging + flatpak CI release pipeline" (targets `main`,
18 files / 13 commits). Reviewed against the two concerns: does the **initial v0.1.0
release** fire correctly, and is it **mirrored to GitHub** as expected.

## TL;DR

- **GitLab release path: sound and proven.** The combined branch pipeline (#555) is
  fully green — `build:flatpak` (4m36s), `test:compile`, `changelog-check` — so the
  flatpak builds with all of !65's changes. Merging cuts `v0.1.0` and publishes the
  bundle. **One correction to earlier guidance: merge with NO `bump:*` label** (below).
- **GitHub mirror: will NOT happen as-is.** `GH_TOKEN` is missing → `release:github`
  fails outright. Two more provisioning steps are also pending. The plan doc already
  flags this ("GitHub mirror provisioning (pending — not yet done)").

## 1. Initial release (GitLab) — works, with one correction

**Flow on merge (no bump label):** main-push pipeline runs → `tag-release` (`needs: []`,
no prior tag required) sees `VERSION=0.1.0` + a `## [0.1.0]` CHANGELOG section + no tag
→ creates `v0.1.0`. The tag pipeline then runs `build:flatpak` → `release:gitlab`
(bundle → generic package registry → GitLab Release with the 0.1.0 notes).

**CORRECTION (my earlier guidance was wrong): do NOT add a `bump:*` label for v0.1.0.**
The bootstrap is `tag-release`, not the bump-label flow — `auto-release` *requires a
prior `v*` tag to diff against* and can't cut the first release (plan §Bootstrap). A
bump label would only invoke `auto-release`, which no-ops on the missing prior tag;
`tag-release` cuts `v0.1.0` regardless. Cleanest path: **merge with no label.** From
v0.1.0 onward, the bump-label flow owns versioning.

**Verified good:**
- Pipeline #555 green on the combined tree — the build is the real gate and it passes.
- Release-notes `awk` correctly extracts the whole `## [0.1.0]` section, including the
  folded-in native-packaging bullets.
- Manifest-referenced build files all present (`flatpak/launch.sh`, `icon_256.png`,
  `launch.desktop`, `metainfo.xml`); the CI-manifest derivation (`make_ci_manifest.py`)
  correctly swaps the Deckard module to the local `git archive` export.
- Protected `v*` tags (create: Maintainers) and `RELEASE_BOT_TOKEN` (protected+masked)
  are present, so `tag-release` can create the protected tag and the tag pipeline sees
  protected vars.
- **VERSION↔About end-to-end:** the tag commit has `VERSION=0.1.0`, the flatpak copies
  it into `/app/bin/Deckard/VERSION`, so `gl.deckard_version` → the About dialog shows
  "0.1.0" in the released flatpak too. !65's changes don't affect flatpak runtime
  (`is_flatpak()` keeps the old data path; the XDG migration no-ops under flatpak).

## 2. GitHub mirror — 3 provisioning steps pending (the plan says so too)

`release:github` runs only on `v*` tags, `needs release:gitlab`, and mirrors the
release object + `.flatpak` asset to `github.com/nazbert/Deckard` via the REST API
(idempotent; pushes the tag itself rather than trusting the async mirror). The job
logic is correct — but its prerequisites are not in place:

1. **BLOCKER — `GH_TOKEN` is not set.** The job asserts `: "${GH_TOKEN:?…}"` and will
   fail immediately. Only `RELEASE_BOT_TOKEN` exists in the project variables. Add
   `GH_TOKEN` as **masked + protected** (fine-grained PAT, **Contents: read/write** on
   `nazbert/Deckard`). Protected `v*` tags already exist, so it will be exposed on the
   tag pipeline once added.
2. **Push mirror not configured** (GitLab → GitHub). Strictly, `release:github` is
   self-sufficient for the tag+release+asset (it pushes the tag directly, and the fork
   already has `5c0e15d2` as an ancestor, so the delta pushes cleanly). But without the
   mirror the fork's **`main` branch stays stale** — and the committed manifest builds
   the Deckard module from `github.com/nazbert/Deckard` `main`, so a from-source/flathub
   build would get old code. Configure the push mirror (Settings → Repository →
   Mirroring) so branches stay in sync.
3. **Fork still carries `.github/workflows/release.yaml`** (confirmed HTTP 200) — the
   inherited upstream go-semantic-release workflow, a divergent release path that
   derives versions from commit messages, not `VERSION`. Delete it on the fork so the
   mirror is the only thing cutting GitHub releases. (Plan claims it's
   `workflow_dispatch`-only, so it won't auto-fire — but verify that, or just delete.)

## 3. Correctness notes (non-blocking)

- **`tag-release` tags before the build gate.** It has `needs: []`, so on the bootstrap
  it creates `v0.1.0` without waiting for `build:flatpak` to prove the bundle builds
  (unlike `auto-release`, which `needs build:flatpak`). If the build were broken you'd
  get a dangling tag + a failed tag-pipeline and no release. **Mitigated:** #555's
  `build:flatpak` is green on this exact tree, so the tag pipeline's build will pass.
- **`release-cli create` is not idempotent.** A retry of `release:gitlab` after the
  release exists would fail (unlike `release:github`, which reconciles). Fine for a
  first release; a retry wart.
- **`test:compile` references `permissons.py`, which doesn't exist** (typo, no such
  top-level module). `compileall` silently skips it (#555 passed), so it's harmless —
  but it means a real top-level permissions module would go unchecked. Drop the token
  (and note the file list omits `appinfo.py`, `cli_args.py`, `rebrand_migration.py`).
- **No push mirror + direct tag push** means the fork gets a `v0.1.0` tag pointing 13
  commits ahead of its stale `main` — valid but odd until the mirror lands.

## 4. Pre-release checklist

Before merging (to make both the release and the mirror work):

- [ ] Add **`GH_TOKEN`** CI variable — masked + protected, fine-grained PAT, Contents:rw
      on `nazbert/Deckard`. *(mirror blocker)*
- [ ] Configure the **push mirror** GitLab → `github.com/nazbert/Deckard` (same PAT).
- [ ] **Delete `.github/workflows/release.yaml`** on the fork.
- [ ] Confirm `RELEASE_BOT_TOKEN`'s identity can create the protected `v*` tag
      (Maintainer) and the project is on ci-automation's job-token allowlist.
- [ ] (optional) drop the `permissons.py` token from `test:compile`.

At merge:

- [ ] **Merge with NO `bump:*` label** — `tag-release` cuts `v0.1.0`.
- [ ] Watch the `v0.1.0` tag pipeline: `build:flatpak` → `release:gitlab` (bundle on the
      GitLab Release) → `release:github` (release + asset on the fork).
- [ ] Verify: GitLab Release `Deckard v0.1.0` with `deckard-0.1.0-x86_64.flatpak`; GitHub
      release with the identical asset; the flatpak's About shows "0.1.0".

## Verdict

The **GitLab release will work** once merged without a bump label — the build is proven
and the wiring is correct. The **GitHub mirror will not** until `GH_TOKEN` (blocker) and
the two other provisioning steps are done. None of these are code bugs in the MR; they
are the pre-release provisioning the plan already lists as pending.
