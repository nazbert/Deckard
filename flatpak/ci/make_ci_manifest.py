#!/usr/bin/env python3
"""Derive the CI flatpak manifest from the committed one.

Usage: make_ci_manifest.py <manifest.yml> <src-dir-relative-to-manifest>

The committed manifest builds the app module from the GitHub repo of the fork,
which is right for flathub and wrong for CI. This script rewrites the manifest
in place, and the output is a throwaway build input that nobody commits.
"""
import sys

import yaml


def main() -> int:
    manifest_path, src_dir = sys.argv[1], sys.argv[2]
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    # Swap the sources of the Deckard module for a local directory, the clean
    # git archive export that .gitlab-ci.yml stages beside the manifest, and
    # leave every other module alone. flatpak/install.sh makes the same rewrite
    # with yq for a local build.
    for module in manifest["modules"]:
        if isinstance(module, dict) and module.get("name") == "Deckard":
            module["sources"] = [{"type": "dir", "path": src_dir}]
            break
    else:
        print("error: no 'Deckard' module in the manifest", file=sys.stderr)
        return 1

    # The rewrite happens in place, and it loses the comments and the
    # formatting of the source manifest.
    with open(manifest_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, width=100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
