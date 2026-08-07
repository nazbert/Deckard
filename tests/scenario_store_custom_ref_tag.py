"""
Custom plugins pinned to a TAG must install (gl#197).

clone_repo used `git switch <ref>` to move the staged clone onto the
configured ref. `switch` refuses tags (and any other detachable ref)
without --detach -- and since subp_call's return code is ignored there,
the clone silently stayed on the default-branch tip and THAT tree was
swapped into place: a custom plugin configured with `branch: "v1"` (a tag,
the natural way to pin a release) installed the wrong code with no error.
`git checkout <ref>` handles branches and tags alike.

Exercised WITHOUT network: the "remote" is a local fixture repository
(git clone accepts a path), same offline pattern as the other store
scenarios. The harness runs with --devel (fixtures.py), so download_repo
dispatches straight into clone_repo -- the exact path a custom-plugin
prepare/install takes on a devel setup.

Assertions are on the CONTENT of the installed tree, not just the return
code -- with the old `switch` the install still "succeeded" (200), just
with the wrong tree, so only content catches the bug.
"""
import os
import subprocess

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend


CACHE_DIR = os.path.join(gl.DATA_PATH, "cache")
FIXTURE_REPO = os.path.join(gl.DATA_PATH, "fixture-repo")


def _isolate_git_config() -> None:
    """clone_repo appends safe.directory entries with `git config --global`.
    Point the global config at a file inside the harness temp dir so the
    scenario never touches the real ~/.gitconfig, and pin identity/system
    config for the fixture commits."""
    os.environ["GIT_CONFIG_GLOBAL"] = os.path.join(gl.DATA_PATH, "gitconfig")
    os.environ["GIT_CONFIG_SYSTEM"] = os.devnull
    os.environ["GIT_AUTHOR_NAME"] = "harness"
    os.environ["GIT_AUTHOR_EMAIL"] = "harness@example.invalid"
    os.environ["GIT_COMMITTER_NAME"] = "harness"
    os.environ["GIT_COMMITTER_EMAIL"] = "harness@example.invalid"


def _git(*args: str) -> None:
    subprocess.run(["git", "-C", FIXTURE_REPO, *args], check=True,
                   capture_output=True, text=True)


def _write(name: str, content: str) -> None:
    with open(os.path.join(FIXTURE_REPO, name), "w") as f:
        f.write(content)


def make_fixture_repo() -> None:
    """History:  commit1 (tag v1, "tagged content")
                 -> commit2 on main ("main content", the default tip)
                 -> branch feature off main ("feature content")."""
    os.makedirs(FIXTURE_REPO)
    subprocess.run(["git", "init", "-b", "main", FIXTURE_REPO], check=True,
                   capture_output=True, text=True)

    _write("manifest.json", '{"id": "com_test_TagPlugin"}')
    _write("flag.txt", "tagged content")
    _git("add", "-A")
    _git("commit", "-m", "release v1")
    _git("tag", "v1")

    _write("flag.txt", "main content")
    _git("commit", "-am", "post-release drift on main")

    _git("checkout", "-b", "feature")
    _write("flag.txt", "feature content")
    _git("commit", "-am", "feature work")
    _git("checkout", "main")


def _flag(dest: str) -> str:
    with open(os.path.join(dest, "flag.txt")) as f:
        return f.read()


def _staging_leftovers() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    return [e for e in os.listdir(CACHE_DIR) if e.startswith(".clone-staging.")]


def test_tag_ref_installs_tagged_tree(sb: StoreBackend) -> None:
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_TagPlugin")

    # Through download_repo: --devel (harness default) routes to clone_repo,
    # like a real custom-plugin install on a devel setup. expected_id proves
    # the manifest gate reads the TAG's staged tree.
    result = sb.download_repo(repo_url=FIXTURE_REPO, directory=dest,
                              branch_name="v1", expected_id="com_test_TagPlugin")

    assert result == 200, f"install pinned to a tag must succeed, got {result!r}"
    assert _flag(dest) == "tagged content", (
        f"installed tree is not the tagged revision (got {_flag(dest)!r}) -- "
        f"`git switch <tag>` fails silently and leaves the default tip"
    )
    with open(os.path.join(dest, "VERSION")) as f:
        assert f.read() == "v1"
    assert _staging_leftovers() == [], (
        f"staging litter left in cache: {_staging_leftovers()}"
    )
    print("PASS: a custom plugin pinned to a tag installs the tagged tree")


def test_branch_ref_still_installs_branch_tip(sb: StoreBackend) -> None:
    # Regression guard: checkout must keep handling plain branches.
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_BranchPlugin")

    result = sb.clone_repo(repo_url=FIXTURE_REPO, local_path=dest,
                           branch_name="feature")

    assert result == 200, f"install pinned to a branch must succeed, got {result!r}"
    assert _flag(dest) == "feature content", (
        f"installed tree is not the branch tip: {_flag(dest)!r}"
    )
    with open(os.path.join(dest, "VERSION")) as f:
        assert f.read() == "feature"
    print("PASS: a custom plugin pinned to a branch still installs the branch tip")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_custom_ref_tag")
    _isolate_git_config()
    make_fixture_repo()

    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)

    test_tag_ref_installs_tagged_tree(sb)
    test_branch_ref_still_installs_branch_tip(sb)
    print("PASS: scenario_store_custom_ref_tag")


if __name__ == "__main__":
    main()
