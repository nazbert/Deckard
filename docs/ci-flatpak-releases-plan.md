# CI: flatpak release pipeline — design (issue #128)

**Status: implemented on branch `ci/flatpak-releases`** (this doc rides along in the
MR; the same plan is posted as a note on #128).

Goal: GitLab CI on this fork that produces installable flatpak releases, reusing
the nb-labs/ci-automation bump-labeled-MR release bot already running on
airsensor / netviz / netviz-collector.

## Release model

Identical to the other consumers: merging a `bump:major|minor|patch`-labeled MR to
`main` IS the release. `auto-release` stamps the version + CHANGELOG.md, commits
`release: vX.Y.Z`, pushes with `-o ci.skip`, and tags `vX.Y.Z`; the tag pipeline
builds the flatpak bundle, creates a GitLab Release with it attached, and
mirrors that release to the GitHub fork (see Key decisions).
`tag-release` remains the manual-fallback (and the bootstrap path, below).

## Key decisions

- **Fork version home = root `VERSION` file** (custom inline adapter in
  `release.config.mjs`), starting its own line at `0.1.0`. NOT
  `globals.py:app_version` ("1.5.0-beta.15"): that value is upstream-aligned and
  plugins gate on it for compatibility checks, so the bot must never rewrite it.
  `VERSION` was an empty, unused upstream leftover (the StoreBackend `VERSION`
  reads are per-plugin files in cloned store repos) — the fork claims it.
  ci-automation's `next-version` only speaks clean `vX.Y.Z`, which an independent
  fork line satisfies and upstream's `-beta.N` scheme would not.
- **CHANGELOG.md** (new, Keep-a-Changelog-ish with `## [Unreleased]`) is the bot's
  changelog. The empty upstream `CHANGELOG` file is left untouched.
- **Build from the CI checkout, not upstream's tag.** The committed manifest's
  app module sources upstream GitHub at a pinned tag (currently 1.5.0-beta.14 —
  stale even against the tree). `flatpak/ci/make_ci_manifest.py` derives a
  throwaway CI manifest with that module's sources swapped to
  `{type: dir, path: src}`, where `src` is a clean `git archive` export of
  `$CI_COMMIT_SHA`. This mirrors what `flatpak/install.sh` does with yq, without
  a per-job yq download from GitHub (this IP gets 429-limited there).
- **flathub `shared-modules` is cloned at build time** — the manifest references
  `shared-modules/libusb/libusb.json` but the repo carries no submodule.
- **Builder image**: `quay.io/gnome_infrastructure/gnome-runtime-images:gnome-50`
  (verified to exist, rebuilt daily) — flatpak-builder + org.gnome.{Platform,Sdk}//50
  preinstalled, matching the manifest's `runtime-version: '50'`.
- **Runner**: `tags: [flatpak]` — a dedicated **privileged** runner
  (`run_untagged=false`, so privilege reaches only jobs that ask for the tag).
  Field finding (#128): userns creation alone isn't enough — bwrap must mount a
  fresh /proc inside its userns, and docker's masked /proc trips the kernel's
  locked-mounts rule for *any* unprivileged container. `cap_add=SYS_ADMIN` was
  tested and ruled out (the gnome image runs as uid 1000; caps never become
  effective), leaving privileged as the only working arrangement — same as
  GNOME's and flathub's own builders. `--disable-rofiles-fuse` stays, keeping
  the build independent of FUSE availability.
- **When builds run**: always on `main` and `v*` tags; on MRs automatically only
  when packaging inputs change (`manifest`, `pypi-requirements.yaml`,
  `flatpak/**`, `.gitlab-ci.yml`), manual+non-blocking otherwise — cold-cache
  builds take tens of minutes. `auto-release` `needs:` the build, so a broken
  bundle can never be tagged as a release.
- **Test gate**: `test:compile` byte-compiles the tree (syntax-level only;
  imports need GTK4/PyGObject). Honest but thin — the flatpak build is the real
  gate. Running the scenario harness (`tests/run_all.py`) in CI is follow-up
  work, tracked on #128.
- **Release artifact**: `deckard-<X.Y.Z>-x86_64.flatpak` uploaded to
  the generic package registry (durable) and linked as a package asset on the
  GitLab Release; notes = that version's CHANGELOG.md section (awk extraction,
  release-cli driven directly — the declarative `release:description` is
  shell-expanded by GitLab; airsensor audit-X16 lesson). Branch/MR builds name
  bundles `<VERSION>+<shortsha>` and live as 2-week CI artifacts.
- **Mirror the release to the GitHub fork.** Once the GitLab Release is cut,
  `release:github` reproduces it on `github.com/nazbert/Deckard`. Split model: a
  GitLab *push mirror* (Settings → Repository) carries branches + tags, but moves
  git refs **only** — so the job adds what a mirror can't, the Release object and
  the flatpak asset, via the GitHub REST API (`curl`+`jq`, no `gh` dependency).
  `needs: release:gitlab`, so GitHub is only ever cut for releases GitLab also got
  (GitLab stays the source of truth); it pushes the tag itself (`GIT_DEPTH: 0`)
  rather than trusting the async mirror's timing, and is idempotent (reuses an
  existing release / replaces the asset) so a retry or re-tag reconciles instead
  of `422`-ing. Requires, still to be provisioned manually (see below): the push
  mirror, a masked+protected `GH_TOKEN`, and removal of the inherited
  `.github/workflows/release.yaml`.

## One-time project provisioning (done via API alongside this MR)

1. Labels: `bump:major`, `bump:minor`, `bump:patch`, `no-changelog`.
2. Protected tag `v*` (create: Maintainers) — also gates protected variables.
3. Protected branch `main` push access for Maintainers (the bot pushes the
   release-stamp commit).
4. `RELEASE_BOT_TOKEN` project variable (masked + protected): project access
   token `release-bot`, Maintainer role, scopes `api` + `write_repository`.
5. Deckard (project 15) on nb-labs/ci-automation's CI job-token
   allowlist (the `.release` jobs clone it with `CI_JOB_TOKEN`).
6. Instance runner `flatpak-privileged` (id 44) registered in the gitlab-runner
   container on hugo: docker executor, `privileged = true`, tag `flatpak`,
   `run_untagged = false`, limits mirroring the buildkit runner.

## GitHub mirror provisioning (pending — not yet done)

For `release:github` to publish anything:

1. **Push mirror** on the GitLab project (Settings → Repository → Mirroring
   repositories → Push) → `https://github.com/nazbert/Deckard.git`, a GitHub PAT
   as the password. Mirrors branches + tags on every push.
2. **`GH_TOKEN`** project variable (masked + **protected**): a fine-grained PAT
   scoped to the fork repo, **Contents: read/write**. Protected so it is exposed
   only on protected `v*` tag pipelines. The same PAT can serve as the mirror
   password.
3. **Delete `.github/workflows/release.yaml`** on the fork — the inherited
   upstream `go-semantic-release` workflow. `workflow_dispatch`-only so it won't
   auto-fire, but it derives versions from commit messages instead of `VERSION`,
   i.e. a divergent second release path; the mirror should be the only thing
   cutting GitHub releases.

## Bootstrap (first release)

No `v*` tag exists on this repo, and `auto-release` requires one to diff
against. This MR therefore hand-stamps `VERSION=0.1.0` + a `## [0.1.0]`
CHANGELOG section; on merge, `tag-release` (which needs no prior tag) cuts
`v0.1.0` and the tag pipeline publishes the first bundle release. From then on
the bump-label flow owns versioning.

Rehearsal: the MR's own pipeline runs `build:flatpak` automatically (it changes
`.gitlab-ci.yml`), validating the entire flatpak build before merge.
`DRY_RUN=1` on `auto-release` remains available for bot rehearsal on main.

## Install / consume

Download the `.flatpak` asset from the GitLab Release, then
`flatpak install ./deckard-<ver>-x86_64.flatpak` (runtime dependency
`org.gnome.Platform//50` resolves from flathub). Bundle installs don't
auto-update — an ostree repo channel would fix that; see follow-ups.

## Follow-ups (not in this MR)

- Run the scenario harness in CI (needs a GTK4/PyGObject-capable image; the
  gnome-runtime-images SDK could host it inside a `flatpak build` shell).
- Publish an ostree repo channel (e.g. NAS + static HTTP) so installed forks
  auto-update via `flatpak update` instead of one-shot bundles.
- Advisory drift check: `pypi-requirements.yaml` vs `requirements.txt`
  (req2flatpak regeneration nudge).
- aarch64 bundles if ever needed (manifest wheels already pinned for both
  arches; would need an arm runner or qemu).
