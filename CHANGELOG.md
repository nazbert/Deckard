# Changelog

Notable changes to this fork. Versions are the fork's own release line (root
`VERSION` file, `vX.Y.Z` tags), independent of upstream StreamController's
`app_version` in `globals.py`. Releases are cut by merging a
`bump:major|minor|patch`-labeled MR to `main` (nb-labs/ci-automation); each
release publishes an installable flatpak bundle on the GitLab Release.

## [Unreleased]

## [0.1.0] - 2026-07-12

### Added

- GitLab CI pipeline: byte-compile test gate, flatpak bundle build from the
  CI checkout on the unconfined runner, GitLab Releases with the bundle
  attached on `v*` tags, and bump-labeled-MR release automation via
  nb-labs/ci-automation (#128).
