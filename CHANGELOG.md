# Changelog

Notable changes to this fork. Versions are the fork's own release line (root
`VERSION` file, `vX.Y.Z` tags), independent of upstream StreamController's
`app_version` in `globals.py`. Each release publishes an installable flatpak
bundle as a release asset.

## [Unreleased]

## [0.1.0] - 2026-07-14

### Added

- First Deckard release: an installable flatpak bundle is built and published
  as a release asset on every `vX.Y.Z` tag.
- Native Arch-family package (`deckard-git`) for the AUR, alongside the flatpak.
- Native installs run as the `deckard` command and store their data under
  `$XDG_DATA_HOME/deckard` (`~/.local/share/deckard`), migrated automatically
  from the previous `~/.var/app` location.
- About dialog shows the Deckard fork release version (from the `VERSION`
  file); the upstream StreamController base is noted in the About comments.
