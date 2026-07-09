"""
Regression test for the config_gen stamping race behind "VolumeMixer icons
blank on the second page".

load_page() must stamp every input's config_gen to the new generation
SYNCHRONOUSLY -- under _page_gen_lock, at the generation bump -- not only via
the asynchronous per-input _load_input_if_current on the load pool. Paints are
triggered from other threads (Page.initialize_actions on the action pool, the
tick loop, update_all_inputs) that read controller_input.config_gen directly
(ControllerKey.update, DeckController.py ~3312). If a paint reads config_gen
after the generation bumped but before the async stamp caught up, it carries
the PREVIOUS generation, and the present-boundary judge drops it as stale-gen
-- silently blanking the newly loaded page's own keys. Observed live: the
VolumeMixer overlay's own keys dropped with task_gen=N, current_gen=N+1.

Deterministic seam: make load_all_inputs a no-op so the ONLY thing that can
advance config_gen is the synchronous stamp under test, then load a page and
assert every input carries the new generation the instant load_page returns.
(With the fix reverted this fails: the inputs keep the previous generation.)
"""
import fixtures
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_config_gen_stamp")
    controller = fixtures.make_headless_controller(serial="cfggen-1")
    try:
        # Neutralize the async per-input stamp path: with load_all_inputs a
        # no-op, config_gen can only advance via the synchronous stamp in
        # load_page -- which is exactly what the real paint path races.
        controller.load_all_inputs = lambda *a, **k: None

        seed_path = fixtures.seed_page("CfgGenPage")
        page = gl.page_manager.get_page(seed_path, controller)

        prev_gen = controller._page_load_generation
        controller.load_page(page, allow_reload=True)
        gen = controller._page_load_generation
        assert gen == prev_gen + 1, f"expected a generation bump, got {prev_gen} -> {gen}"

        total = 0
        stale = []
        for input_type in controller.inputs:
            for inp in controller.inputs[input_type]:
                total += 1
                if inp.config_gen != gen:
                    stale.append((str(inp.identifier), inp.config_gen))

        assert not stale, (
            f"{len(stale)}/{total} input(s) still carry a stale config_gen after "
            f"load_page returned (current gen={gen}): {stale[:8]} -- a paint "
            f"reading this would be dropped by the present-boundary judge as "
            f"stale-gen, blanking the new page's own keys"
        )
        print(f"PASS: all {total} inputs stamped config_gen={gen} synchronously at the gen bump")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_config_gen_stamp")


if __name__ == "__main__":
    main()
