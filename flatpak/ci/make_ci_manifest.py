#!/usr/bin/env python3
"""Derive the CI flatpak manifest from the committed one.

The committed manifest (com.core447.StreamController.yml) builds the app
module from upstream's GitHub repo at a pinned tag — right for flathub,
wrong for CI, which must build the commit under test. This swaps the
StreamController module's sources for a local directory (a clean
`git archive` export staged by .gitlab-ci.yml, relative to the manifest)
and leaves every other module untouched. Same rewrite flatpak/install.sh
performs with yq for local builds.

Usage: make_ci_manifest.py <manifest.yml> <src-dir-relative-to-manifest>
Rewrites <manifest.yml> in place. Comments/formatting are not preserved —
the output is a throwaway build input, never committed.
"""
import sys

import yaml


def main() -> int:
    manifest_path, src_dir = sys.argv[1], sys.argv[2]
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    for module in manifest["modules"]:
        if isinstance(module, dict) and module.get("name") == "StreamController":
            module["sources"] = [{"type": "dir", "path": src_dir}]
            break
    else:
        print("error: no 'StreamController' module in the manifest", file=sys.stderr)
        return 1

    with open(manifest_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, width=100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
