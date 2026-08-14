#!/usr/bin/env python3
"""Derive the CI flatpak manifest from the committed one.

The committed manifest, io.github.nazbert.Deckard.yml, builds the app module
from the GitHub repo of the fork. That is right for flathub and wrong for CI,
which must build the commit under test. This script swaps the sources of the
Deckard module for a local directory, a clean git archive export that
.gitlab-ci.yml stages beside the manifest, and it leaves every other module
alone. flatpak/install.sh makes the same rewrite with yq for a local build.

Usage: make_ci_manifest.py <manifest.yml> <src-dir-relative-to-manifest>

It rewrites <manifest.yml> in place. The output loses the comments and the
formatting, because it is a throwaway build input that nobody commits.
"""
import sys

import yaml


def main() -> int:
    manifest_path, src_dir = sys.argv[1], sys.argv[2]
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    for module in manifest["modules"]:
        if isinstance(module, dict) and module.get("name") == "Deckard":
            module["sources"] = [{"type": "dir", "path": src_dir}]
            break
    else:
        print("error: no 'Deckard' module in the manifest", file=sys.stderr)
        return 1

    with open(manifest_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, width=100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
