"""
Scenario: the render engine reads a NAMED, CLOSED set of `gl` slots.

Sibling of scenario_headless_engine_no_gtk, one layer down. That scenario pins
what the engine closure may IMPORT; this one pins what it may READ off the
shared `globals` namespace. Same architecture -- fail at the offending act,
sweep afterwards as belt and braces, and prove the instrument was armed so no
assertion can pass vacuously -- but the subject is attribute access, so the
tripwire is a recording module rather than a meta-path finder.

WHY A LIST OF SLOT NAMES IS WORTH GUARDING

`gl` is a flat namespace of late-initialised services, and reading one is an
invisible dependency edge: nothing declares it, nothing resolves it, nothing
notices when a new one appears. The engine currently reads no locale manager,
no App, no notification facade, no store -- it is a headless render core that
happens to share a namespace with the UI. That separation is true by accident
and defended by nothing, which is exactly the condition under which it decays.

ALLOWED_ENGINE_GL_SURFACE below turns it into a decision. Engine code reaching
for a new slot fails this scenario until someone edits the list, and the edit
is where the question "should the render path depend on that?" gets asked. The
guard's value is the conversation, not the assertion.

TWO LAYERS, DIFFERENT BLIND SPOTS

* The RUNTIME recorder swaps ``globals.__class__`` for a ModuleType subclass
  whose ``__getattribute__`` records (slot, caller file) -- the documented
  module-properties mechanism. It sees what a real page load, a real action,
  real input and a real teardown actually touch, including reads through
  helpers no static reading would connect to the engine. It is blind to
  branches the drive does not enter.
* The STATIC sweep AST-walks the source of every engine module the drive
  loaded and collects every ``gl.X`` it mentions. It sees the ``except``
  branch nobody took. It is blind to ``getattr(gl, name)`` and to alias
  indirection -- the same documented non-detections scripts/check_gl_slots.py
  carries, and for the same reason: an AST cannot resolve a computed name.

Neither layer is sufficient; together they cover both ways a read hides.

WHAT COUNTS AS AN ENGINE FILE

Any loaded module whose source lives under src/ or GtkHelper/, plus globals.py
and cli_args.py. Everything else -- the harness, the standard library, the
site packages -- is not engine code and its reads are not the engine's. The
harness in particular rebinds and reads several slots to stand the engine up,
and attributing that to the engine would make the list a fiction.

``src/backend/startup_queue.py`` counts, because the engine imports it: the
deck controller claims CLI-parked page and state requests through it. That
module also hosts leg A, the pre-App notification deferral, which mentions
``gl.app`` and ``gl.app_loading_finished_tasks`` -- two of the REQUIRED-ABSENT
slots. So the static layer exempts those two names IN THAT ONE FILE (see
LEG_A_HOST_EXEMPTION) while the runtime layer exempts nothing: the code is
reachable from the module the engine imports, but the engine never calls it,
and a drive that ever DID reach leg A would fail here.

``src/backend/services.py`` is the mirror image and deliberately absent: it is
UI-side (only GtkHelper imports it), and its ``tr()`` reads ``gl.lm``. If any
engine module ever imported it, the static sweep would load services.py as an
engine file, see ``lm``, and rightly trip -- which is the guard doing its job,
not a false positive.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import ast  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from contextlib import contextmanager  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

# Imported for the same reason scenario_headless_engine_no_gtk imports it: the
# harness substitutes a stub DeckManager, so without this the real module --
# a genuine part of the engine closure, and a reader of four slots -- would
# never be parsed by the static sweep.
import src.backend.DeckManagement.DeckManager  # noqa: E402,F401

WATCHDOG_SECONDS = 90

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Roots whose files ARE the engine when they appear as a caller or as a loaded
# module. Directories are matched by prefix; the two bare modules by name.
_ENGINE_DIRS = ("src", "GtkHelper")
_ENGINE_FILES = ("globals.py", "cli_args.py")

SERIAL = "gl-surface-1"

# --------------------------------------------------------------------- #
# THE LIST                                                               #
# --------------------------------------------------------------------- #
#
# Every slot the engine may read, each tagged with the subsystem that reads it
# so an edit here is an informed one rather than a name appended to make a red
# scenario green. Stamped from a measured run of this file's own two layers
# over this tree -- and that is how it must be RE-stamped, because a list
# smaller than reality is a permanently red scenario (run_all.py has no
# expected-failure culture) and a list larger than reality guards nothing. The
# failure messages below print what was measured, so re-stamping is a read of
# the diff rather than a new investigation.
#
# Adding an entry is the conversation this guard exists to force. The likeliest
# future applicant is the locale manager: if engine code adopts
# src.backend.services.tr(), `lm` arrives here, and "should the render path
# speak the user's language" is a real question with a real answer -- it just
# has to be asked out loud.
ALLOWED_ENGINE_GL_SURFACE = frozenset({
    "DATA_PATH",                   # paths
    "STATIC_SETTINGS_FILE_PATH",   # settings
    "api_page_requests",           # cli
    "api_state_requests",          # cli
    "argparser",                   # flags
    "deck_manager",                # decks
    "fallback_font",               # labels
    "icon_pack_manager",           # icons
    "image_extensions",            # media
    "media_manager",               # thumbnails
    "page_manager",                # pages
    "plugin_manager",              # plugins
    "presence_monitor",            # screensaver
    "screen_locked",               # lockscreen
    "settings_manager",            # settings
    "signal_manager",              # signals
    "svg_extensions",              # media
    "threads_running",             # shutdown
    "top_level_dir",               # assets
    "video_extensions",            # media
    "window_grabber",              # autoswitch
})

# One file, two slots, static layer only. The deck controller imports the
# startup queue for the CLI-request legs; leg A of that same module is the
# App's pre-activation deferral and names `gl.app` and
# `gl.app_loading_finished_tasks`. The names are in the engine's import
# closure but not on any path the engine walks, so the exemption is scoped as
# narrowly as it can be: this file, these names, and never at runtime.
# check_static_absent() fails if the exemption goes stale.
LEG_A_HOST_EXEMPTION = {
    "src/backend/startup_queue.py": frozenset({"app", "app_loading_finished_tasks"}),
}

# The seam, named. A subset check against a list anyone may extend does not on
# its own defend "the engine reads no UI"; these five do. Extending
# ALLOWED_ENGINE_GL_SURFACE with one of them is not enough to make this
# scenario green -- that takes deleting it from here, which is a much louder
# edit.
REQUIRED_ABSENT = frozenset({
    "lm",                          # locale
    "app",                         # application
    "notify",                      # notifications
    "store_backend",               # store
    "app_loading_finished_tasks",  # boot deferral
})

# Slots the workload cannot fail to read. If the recorder does not install, or
# the drive does not run, every subset and disjointness claim above holds
# trivially -- these turn that silent pass into a failure.
REQUIRED_OBSERVED = frozenset({
    "page_manager",
    "settings_manager",
    "api_page_requests",
    "api_state_requests",
})


# --------------------------------------------------------------------- #
# The runtime recorder                                                   #
# --------------------------------------------------------------------- #

_READS: set = set()


class _RecordingGlobals(types.ModuleType):
    """`globals` with every non-dunder attribute read written down."""

    def __getattribute__(self, name: str):
        if not (name.startswith("__") and name.endswith("__")):
            _READS.add((name, sys._getframe(1).f_code.co_filename))
        return super().__getattribute__(name)


@contextmanager
def _recording():
    """Route reads of `gl` through the recorder for the duration."""
    original = type(gl)
    gl.__class__ = _RecordingGlobals
    try:
        yield
    finally:
        gl.__class__ = original


def _repo_relative(path: str) -> str | None:
    absolute = os.path.abspath(path)
    if not absolute.startswith(_REPO_ROOT + os.sep):
        return None
    return os.path.relpath(absolute, _REPO_ROOT).replace(os.sep, "/")


def _is_engine_file(path: str) -> bool:
    relative = _repo_relative(path)
    if relative is None:
        return False
    if relative in _ENGINE_FILES:
        return True
    return relative.split("/")[0] in _ENGINE_DIRS


def engine_runtime_reads() -> set:
    """(slot, repo-relative file) for every recorded read made by engine code."""
    return {
        (slot, _repo_relative(path))
        for slot, path in _READS
        if _is_engine_file(path)
    }


# --------------------------------------------------------------------- #
# The workload                                                           #
# --------------------------------------------------------------------- #

def _any_action_ready(page) -> bool:
    for by_ident in page.action_objects.values():
        for by_state in by_ident.values():
            for by_index in by_state.values():
                for action in by_index.values():
                    if getattr(action, "on_ready_called", False):
                        return True
    return False


def drive_engine_workload() -> None:
    """The same engine exercise scenario_headless_engine_no_gtk drives -- a
    background page, an action page carrying a real ActionCore subclass, key
    and dial input, a page switch, media ticks, teardown -- plus a CLI-parked
    page request and a CLI-parked state request.

    The parked pair matters because the controller is the engine-side end of
    the startup queue's legs B and C: without them the drive would never read
    gl.api_page_requests / gl.api_state_requests, and those slots would sit in
    the allowed list unwitnessed.
    """
    from src.backend import startup_queue

    queue = startup_queue.get()

    red = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "surface_red.png"), color=(220, 20, 20))
    green = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "surface_green.png"), color=(20, 220, 20))
    page_a_path = fixtures.seed_page_with_background("SurfaceA", red)

    # Before the controller: load_default_page() runs at the end of
    # DeckController.__init__ and would hit an unset plugin_manager.
    fixtures.install_stub_plugin_manager(fixtures.make_latch_action_class(), green)

    # Leg B: parked before the deck exists, which is the only state the CLI can
    # park in. DeckController.__init__'s own load_default_page is the claim.
    fixtures.seed_page("Parked")
    queue.park_page_request(SERIAL, "Parked")

    controller = fixtures.make_headless_controller(serial=SERIAL)
    try:
        assert SERIAL not in gl.api_page_requests, (
            "the controller's first load_default_page did not claim the parked "
            "page request -- leg B's engine-side read went unexercised"
        )

        deck = fixtures.raw_deck(controller)

        page_a = gl.page_manager.get_page(page_a_path, controller)
        controller.load_page(page_a, allow_reload=True)
        assert fixtures.wait_until(
            lambda: len(deck.ops_by_name("set_key_image")) > 0, timeout=10), (
            "page A never reached the device"
        )

        # Input while page A is up: the real event callbacks, hold timers and
        # action dispatch all run.
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)
        if controller.inputs.get(Input.Dial):
            from StreamDeck.Devices.StreamDeck import DialEventType

            deck.fire_dial_event(0, DialEventType.PUSH, True)
            deck.fire_dial_event(0, DialEventType.PUSH, False)

        key_ident = controller.inputs[Input.Key][0].identifier.json_identifier
        page_b_path = fixtures.seed_action_page("SurfaceB", key_ident)

        before_switch = deck.current_seq()
        page_b = gl.page_manager.get_page(page_b_path, controller)
        controller.load_page(page_b, allow_reload=True)
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_key_image" for e in deck.ops_after(before_switch)),
            timeout=10), "page B never reached the device"
        assert fixtures.wait_until(lambda: _any_action_ready(page_b), timeout=10), (
            "page B's action never reached on_ready -- ActionCore init / "
            "initialize_actions / on_ready would be outside this scenario's "
            "read surface"
        )
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)

        # Leg C: peeked, applied, resolved -- all inside load_default_page.
        queue.park_state_request(SERIAL, {
            "page_name": "SurfaceA", "coords": "0,0", "state": 0,
        })
        controller.load_default_page()
        assert SERIAL not in gl.api_state_requests, (
            "the parked state request was never resolved -- leg C's "
            "engine-side reads went unexercised"
        )
    finally:
        fixtures.teardown(controller)


# --------------------------------------------------------------------- #
# Guards                                                                 #
# --------------------------------------------------------------------- #

def check_recorder_records_and_restores() -> None:
    """The instrument itself, before anything is measured with it.

    Two properties. First it records: a read made inside the context shows up
    with this file as its caller. Second -- and this is the one worth a guard
    -- the class swap is undone on the way out EVEN WHEN THE BODY RAISES. A
    failing drive that left the recorder installed would leave every later
    read in the process routed through a test double, and the failure would be
    reported as something else entirely.
    """
    baseline = type(gl)
    probe = ("DATA_PATH", __file__)

    _READS.discard(probe)
    with _recording():
        assert isinstance(gl, _RecordingGlobals), (
            "the __class__ swap did not take -- the recorder is not installed "
            "and every read below would go unrecorded"
        )
        assert gl.DATA_PATH, "fixture sanity: the isolated data dir is unset"
    assert probe in _READS, (
        f"the recorder did not record a read it was installed for: {_READS}"
    )
    assert type(gl) is baseline, (
        f"the recorder survived a clean exit from its context: {type(gl)}"
    )

    boom = RuntimeError("deliberate failure inside the recorded region")
    try:
        with _recording():
            assert isinstance(gl, _RecordingGlobals)
            raise boom
    except RuntimeError as raised:
        assert raised is boom, f"an unrelated error masked the restore check: {raised}"
    else:
        raise AssertionError("the deliberate failure did not propagate")
    assert type(gl) is baseline, (
        f"the recorder survived a FAILING exit from its context -- the finally "
        f"restore is broken: {type(gl)}"
    )

    _READS.discard(probe)
    print("PASS: the recorder records reads and restores globals.__class__ on failure")


def check_recorder_saw_the_engine(runtime_reads: set) -> None:
    """Anti-vacuity. Every assertion below is a subset or disjointness claim,
    and both are trivially true of an empty set -- so a recorder that silently
    failed to install would pass the whole scenario. The drive loads pages
    through gl.page_manager and claims CLI requests through the startup
    queue's slots; if none of that was recorded, nothing was measured."""
    assert runtime_reads, (
        "the recorder captured no engine reads at all -- it did not install, "
        "or nothing engine-side ran, and every assertion in this scenario "
        "would be vacuous"
    )
    slots = {slot for slot, _ in runtime_reads}
    missing = sorted(REQUIRED_OBSERVED - slots)
    assert not missing, (
        f"the drive did not exercise {missing} -- these are read on every page "
        f"load and on both CLI-request legs, so their absence means the "
        f"workload did not run, not that the engine stopped reading them. "
        f"Recorded: {sorted(slots)}"
    )
    controller_page_manager = [
        path for slot, path in runtime_reads
        if slot == "page_manager" and path.endswith("/controller.py")
    ]
    assert controller_page_manager, (
        "no gl.page_manager read was attributed to the deck controller -- the "
        "known-allowed read this guard is calibrated against never happened"
    )
    print(f"PASS: the recorder captured {len(runtime_reads)} engine reads of "
          f"{len(slots)} distinct slots")


def check_runtime_surface(runtime_reads: set) -> None:
    """Layer one: what the engine actually read."""
    unlisted = sorted({
        (slot, path) for slot, path in runtime_reads
        if slot not in ALLOWED_ENGINE_GL_SURFACE
    })
    assert not unlisted, (
        f"engine code read gl slots outside the allowed surface: {unlisted}. "
        f"Either the read belongs somewhere else, or "
        f"ALLOWED_ENGINE_GL_SURFACE gains an entry -- and that edit is the "
        f"decision this guard exists to force."
    )
    print(f"PASS: every engine read is inside the allowed surface "
          f"({len(ALLOWED_ENGINE_GL_SURFACE)} slots)")


def check_required_absent(runtime_reads: set) -> None:
    """The teeth. Five slots named individually, because 'the engine reads no
    UI' is the property worth defending and a subset check against a list
    someone can extend does not defend it on its own.

    No file is exempt here, including the startup queue: leg A's `gl.app` read
    is reachable from a module the engine imports, but the engine never calls
    it, and a drive that reached it would be the engine waiting on the App.
    """
    present = sorted({
        (slot, path) for slot, path in runtime_reads if slot in REQUIRED_ABSENT
    })
    assert not present, (
        f"the engine read a UI-side slot: {present}. The render path is "
        f"supposed to run with no locale manager, no App, no notification "
        f"facade and no store -- that is the daemon/client seam, and this is "
        f"where it stops being true."
    )
    print(f"PASS: none of {sorted(REQUIRED_ABSENT)} is read by engine code")


# --------------------------------------------------------------------- #
# The static sweep                                                       #
# --------------------------------------------------------------------- #

def _globals_aliases(tree: ast.Module, relative: str) -> set:
    """The local name(s) bound to the `globals` module in one file.

    `import globals as gl` is the universal style here. A `from globals import
    X` would put a slot into the file's namespace under a bare name this sweep
    cannot follow, so it fails loudly rather than being skipped -- the same
    fail-closed stance scripts/check_gl_slots.py takes on un-analyzable import
    shapes.
    """
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "globals":
                    aliases.add(name.asname or "globals")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "globals" and node.level == 0:
                raise AssertionError(
                    f"{relative} imports slots out of `globals` directly, which "
                    f"this sweep cannot follow -- use `import globals as gl`"
                )
    return aliases


def static_engine_references() -> dict:
    """{repo-relative file: {slot, ...}} over every engine module now loaded.

    Attribute STORES are collected alongside loads. `gl.X = ...` from the
    engine would be just as much a dependency on X as a read, and the ones
    that exist today (the deck manager's thread flag) are in the list on
    their own merit.
    """
    references: dict = {}
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not _is_engine_file(path):
            continue
        relative = _repo_relative(path)
        if relative in references or not path.endswith(".py"):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
        aliases = _globals_aliases(tree, relative)
        if not aliases:
            continue
        slots = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        }
        if slots:
            references[relative] = slots
    return references


def check_static_surface(references: dict) -> None:
    """Layer two: what the engine's source mentions, taken or not.

    This is what catches a `gl.lm` read added inside an `except` branch the
    drive never enters -- the failure mode the runtime recorder structurally
    cannot see.
    """
    assert references, (
        "no engine module referenced `globals` at all -- the sweep found "
        "nothing to check and would pass vacuously"
    )
    unlisted = []
    for relative, slots in sorted(references.items()):
        exempt = LEG_A_HOST_EXEMPTION.get(relative, frozenset())
        for slot in sorted(slots):
            if slot not in ALLOWED_ENGINE_GL_SURFACE and slot not in exempt:
                unlisted.append(f"{relative}: gl.{slot}")
    assert not unlisted, (
        f"engine sources mention gl slots outside the allowed surface: "
        f"{unlisted}. The runtime drive may never reach them; that is what "
        f"this layer is for."
    )
    print(f"PASS: {len(references)} engine modules mention only allowed slots")


def check_static_absent(references: dict) -> None:
    """The teeth again, statically. The exemption is deliberately per-file and
    per-slot: leg A of the startup queue is the only place in the engine's
    import closure that may name `gl.app`, and only because it is the App's
    own protocol sharing a module with the CLI-request legs the engine does
    use."""
    for relative, slots in sorted(references.items()):
        exempt = LEG_A_HOST_EXEMPTION.get(relative, frozenset())
        offending = sorted((slots & REQUIRED_ABSENT) - exempt)
        assert not offending, (
            f"{relative} mentions UI-side slots {offending}. Engine code does "
            f"not get a locale manager, an App, a notification facade or a "
            f"store -- if this file grew a legitimate need for one, it is not "
            f"engine code any more."
        )
    hosts = sorted(LEG_A_HOST_EXEMPTION)
    for host in hosts:
        assert host in references, (
            f"{host} is exempted from the absent-slot check but is not a loaded "
            f"engine module any more -- a stale exemption is a hole"
        )
        stale = sorted(LEG_A_HOST_EXEMPTION[host] - references[host])
        assert not stale, (
            f"{host} no longer mentions {stale}; drop it from the exemption "
            f"rather than leaving the slot permitted there"
        )
    print(f"PASS: no engine source names a UI-side slot (exempt: {hosts})")


def report_measurement(runtime_reads: set, references: dict) -> None:
    """The measured surface, printed on every run. This is what
    ALLOWED_ENGINE_GL_SURFACE is stamped from, so it stays visible rather than
    hiding behind a flag: a flag that skips the assertions is a way for this
    scenario to pass without checking anything, and there is no version of
    that worth the convenience."""
    print("--- measured engine gl surface ---")
    print(f"  runtime: {sorted({slot for slot, _ in runtime_reads})}")
    static = sorted(set().union(*references.values())) if references else []
    print(f"  static:  {static}")
    print(f"  modules: {len(references)} engine sources reference globals")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_engine_gl_surface")

    check_recorder_records_and_restores()

    with _recording():
        drive_engine_workload()
    assert type(gl) is types.ModuleType, (
        f"the recorder outlived the drive: {type(gl)}"
    )

    runtime_reads = engine_runtime_reads()
    references = static_engine_references()
    report_measurement(runtime_reads, references)

    check_recorder_saw_the_engine(runtime_reads)
    check_runtime_surface(runtime_reads)
    check_required_absent(runtime_reads)
    check_static_surface(references)
    check_static_absent(references)

    print("ALL PASS: scenario_engine_gl_surface")


if __name__ == "__main__":
    main()
