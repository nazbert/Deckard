"""
clone_repo must never install a tree it did not move onto.

git checkout handles a branch and a tag alike, and the return codes of the
checkout and of the reset --hard are both checked, so an unreachable ref or
sha fails the install rather than staging the default tip. The remote is a
local fixture repository, and the assertions read the installed content.
"""
import os
import subprocess

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Ok, Err


CACHE_DIR = os.path.join(gl.DATA_PATH, "cache")
FIXTURE_REPO = os.path.join(gl.DATA_PATH, "fixture-repo")


def _isolate_git_config() -> None:
    """clone_repo appends safe.directory entries with git config --global.
    The global config points at a file in the harness temp dir, so the
    scenario never touches the real ~/.gitconfig."""
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
    """The fixture history. commit1 carries tag v1 and "tagged content",
    commit2 on main is the default tip with "main content", and branch
    feature carries "feature content"."""
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


def _rev(ref: str) -> str:
    out = subprocess.run(["git", "-C", FIXTURE_REPO, "rev-parse", ref],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _flag(dest: str) -> str:
    with open(os.path.join(dest, "flag.txt")) as f:
        return f.read()


def _staging_leftovers() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    return [e for e in os.listdir(CACHE_DIR) if e.startswith(".clone-staging.")]


def test_tag_ref_installs_tagged_tree(sb: StoreBackend) -> None:
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_TagPlugin")

    # Through download_repo. The harness default of --devel routes to
    # clone_repo, like a real custom-plugin install. expected_id proves the
    # manifest gate reads the tag's staged tree.
    result = sb.download_repo(repo_url=FIXTURE_REPO, directory=dest,
                              branch_name="v1", expected_id="com_test_TagPlugin")

    assert isinstance(result, Ok), f"install pinned to a tag must succeed, got {result!r}"
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


def test_branch_ref_installs_branch_tip(sb: StoreBackend) -> None:
    # checkout must keep handling a plain branch.
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_BranchPlugin")

    result = sb.clone_repo(repo_url=FIXTURE_REPO, local_path=dest,
                           branch_name="feature")

    assert isinstance(result, Ok), f"install pinned to a branch must succeed, got {result!r}"
    assert _flag(dest) == "feature content", (
        f"installed tree is not the branch tip: {_flag(dest)!r}"
    )
    with open(os.path.join(dest, "VERSION")) as f:
        assert f.read() == "feature"
    print("PASS: a custom plugin pinned to a branch still installs the branch tip")


def test_nonexistent_ref_fails_install(sb: StoreBackend) -> None:
    # A mistyped pinned ref must fail the install. With the checkout return
    # code ignored, the clone stays on the default tip and the wrong tree
    # installs under the mistyped ref.
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_TypoPlugin")

    result = sb.clone_repo(repo_url=FIXTURE_REPO, local_path=dest,
                           branch_name="v1-typo")

    assert isinstance(result, Err), (
        "a nonexistent pinned ref must fail the install, not silently ship "
        "the default-branch tip"
    )
    assert not os.path.exists(dest), (
        "a failed install must not create the destination dir"
    )
    assert _staging_leftovers() == [], (
        f"staging litter left in cache: {_staging_leftovers()}"
    )
    print("PASS: a nonexistent pinned ref fails the install cleanly")


def test_commit_sha_installs_that_commit(sb: StoreBackend) -> None:
    # A reachable catalog sha must still install, and must install that
    # commit's tree rather than the default tip it was cloned at.
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_ShaPlugin")
    sha = _rev("v1")

    result = sb.clone_repo(repo_url=FIXTURE_REPO, local_path=dest, commit_sha=sha)

    assert isinstance(result, Ok), f"install pinned to a reachable sha must succeed, got {result!r}"
    assert _flag(dest) == "tagged content", (
        f"installed tree is not the pinned commit: {_flag(dest)!r}"
    )
    with open(os.path.join(dest, "VERSION")) as f:
        assert f.read() == sha
    print("PASS: a plugin pinned to a reachable commit sha installs that commit")


def test_unreachable_commit_sha_fails_install(sb: StoreBackend) -> None:
    # A well-formed but unreachable catalog sha passes is_safe_commit_sha
    # and reaches git reset --hard. With that return code ignored the reset
    # fails, staging stays on the default tip, and that tree installs as a
    # success under the unreachable sha.
    dest = os.path.join(gl.DATA_PATH, "plugins", "com_test_GoneShaPlugin")
    gone_sha = "deadbeef" * 5  # 40 lowercase hex, not an object in the repo

    result = sb.clone_repo(repo_url=FIXTURE_REPO, local_path=dest, commit_sha=gone_sha)

    assert isinstance(result, Err), (
        "an unreachable pinned commit sha must fail the install, not "
        "silently ship the default-branch tip"
    )
    assert not os.path.exists(dest), (
        "a failed install must not create the destination dir (and must "
        "never leave a tree stamped with a sha it is not)"
    )
    assert _staging_leftovers() == [], (
        f"staging litter left in cache: {_staging_leftovers()}"
    )
    print("PASS: an unreachable pinned commit sha fails the install cleanly")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_custom_ref_tag")
    _isolate_git_config()
    make_fixture_repo()

    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread

    test_tag_ref_installs_tagged_tree(sb)
    test_branch_ref_installs_branch_tip(sb)
    test_nonexistent_ref_fails_install(sb)
    test_commit_sha_installs_that_commit(sb)
    test_unreachable_commit_sha_fails_install(sb)
    print("PASS: scenario_store_custom_ref_tag")


if __name__ == "__main__":
    main()
