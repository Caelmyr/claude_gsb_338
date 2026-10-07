"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import initial_state, models, report, storage  # noqa: E402
from backend.engine import make_engine  # noqa: E402
from backend.run_manager import manager  # noqa: E402

_ENGINES = ["traffic/ca", "traffic/abm", "ecology/ca", "ecology/abm",
            "epidemic/ca", "epidemic/abm"]


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)


def engines_step() -> None:
    for key in _ENGINES:
        d, m = key.split("/")
        eng = make_engine(d, m, seed=42)
        for _ in range(5):
            eng.step()
        assert eng.step_count == 5, key
        assert len(eng.individuals()) > 0, f"{key} has no individuals"
        stats = eng.stats()
        assert stats, f"{key} produced empty stats"
        snap = eng.snapshot()
        assert snap["step"] == 5
        assert snap["stats"] == stats


def interventions_apply() -> None:
    eng = make_engine("epidemic", "abm", seed=1)
    before = eng.stats()["susceptible"]
    res = eng.apply_intervention({"type": "vaccinate", "params": {"fraction": 1.0}})
    assert res["applied"], res
    assert eng.stats()["susceptible"] == 0
    assert before > 0


def storage_atomic_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        # Redirect the module's DATA_DIR for this isolated check.
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene({"id": "x", "name": "t", "updated_at": "z"})
            assert storage.load_scene("x")["name"] == "t"
            storage.save_step("r1", 0, {"step": 0, "v": 1})
            storage.save_step("r1", 7, {"step": 7, "v": 2})
            assert storage.load_step("r1", 7)["v"] == 2
            assert storage.list_steps("r1") == [0, 7]
        finally:
            storage.DATA_DIR = old


def run_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 200, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        r = manager.step(rid, 6)
        assert r["step"] == 6
        series = manager.get_series(rid)
        assert series[0]["step"] == 0 and series[-1]["step"] == 6
        assert len(manager.get_individuals(rid, 6)) == 200
        # snapshot_interval=2 -> full snapshots persisted at 0,2,4,6
        steps = storage.list_steps(rid)
        assert steps == [0, 2, 4, 6], steps
        rpt = report.generate_report(rid)
        assert rpt["steps"] == 7
    finally:
        manager.delete_run(rid)


def initial_state_import() -> None:
    # CSV parse + Chinese header/state aliases.
    csv_text = "位置,车道,状态,速度\n0,0,停车,0\n5,0,行驶,2\n12,1,停车,0\n"
    parsed = initial_state.parse_layout_text(csv_text, "csv")
    assert parsed.ok, parsed.error
    res = initial_state.validate_layout(
        "traffic", "ca", parsed.rows, {"length": 80, "lanes": 3})
    assert res.ok, res.errors
    assert [v["id"] for v in res.layout] == ["v0000", "v0001", "v0002"]
    assert res.layout[1]["state"] == "moving"

    # Per-row errors name the row and reason: out of bounds + duplicate cell.
    bad = initial_state.parse_layout_text("x,y\n0,0\n0,0\n99,0\n", "csv")
    res2 = initial_state.validate_layout(
        "traffic", "ca", bad.rows, {"length": 80, "lanes": 3})
    assert not res2.ok and len(res2.errors) == 2, res2.errors
    assert any("同一格" in e for e in res2.errors)
    assert any("越界" in e for e in res2.errors)

    # Off-road vehicle and overlap on the ring road.
    res3 = initial_state.validate_layout(
        "traffic", "abm", [{"x": 10, "y": 1}], {"road_length": 1000.0})
    assert not res3.ok and any("道路之外" in e for e in res3.errors)
    res4 = initial_state.validate_layout(
        "traffic", "abm", [{"x": 10}, {"x": 12}], {"road_length": 1000.0})
    assert not res4.ok and any("重叠" in e for e in res4.errors)

    # State/type contradiction (ecology) and SIR aliases (epidemic).
    res5 = initial_state.validate_layout(
        "ecology", "ca", [{"x": 1, "y": 1, "type": "rabbit", "state": "fox"}],
        {"width": 60, "height": 60})
    assert not res5.ok and any("矛盾" in e for e in res5.errors)
    res6 = initial_state.validate_layout(
        "epidemic", "ca",
        [{"x": 1, "y": 1, "state": "感染"}], {"width": 40, "height": 40})
    assert res6.ok and res6.layout[0]["state"] == "infected"


def initial_state_determinism() -> None:
    """Preview == engine == reset snapshot for every model family."""
    cases = [
        ("traffic", "ca", {"length": 80, "lanes": 3},
         [{"x": 3 * i, "y": i % 3} for i in range(10)]),
        ("traffic", "abm", {"road_length": 1000.0, "n": 3},
         [{"x": 0.0}, {"x": 100.0}, {"x": 500.0}]),
        ("ecology", "ca", {"width": 40, "height": 40},
         [{"x": i, "y": i, "type": "rabbit"} for i in range(5)] +
         [{"x": i, "y": i + 1, "type": "fox"} for i in range(3)]),
        ("ecology", "abm", {"width": 400, "height": 400},
         [{"x": 10.0 * i, "y": 20.0, "type": "boid"} for i in range(6)] +
         [{"x": 100.0, "y": 100.0, "type": "predator"}]),
        ("epidemic", "ca", {"width": 30, "height": 30},
         [{"x": 1, "y": 1, "state": "infected"},
          {"x": 2, "y": 2, "state": "recovered"}]),
        ("epidemic", "abm", {"width": 300, "height": 300, "n": 12},
         [{"x": 10.0 * i, "y": 50.0, "state": "infected" if i < 2 else "susceptible"}
          for i in range(12)]),
    ]
    for domain, model, cfg, rows in cases:
        res = initial_state.validate_layout(domain, model, rows, cfg)
        assert res.ok, (domain, model, res.errors)
        eff = {**cfg, **res.config_updates}
        preview = initial_state.build_preview(domain, model, eff, res.layout, seed=7)
        eng = make_engine(domain, model, config=eff, seed=7,
                          initial_state=res.layout)
        assert eng.snapshot() == preview, f"{domain}/{model} preview mismatch"
        # A second independent build must reproduce the exact same snapshot.
        eng2 = make_engine(domain, model, config=eff, seed=7,
                           initial_state=res.layout)
        assert eng2.snapshot() == preview, f"{domain}/{model} not reproducible"
        eng.step(); eng.step()
        assert eng.step_count == 2  # simulation proceeds from the fixed layout

    # Full scene -> run -> reset round trip keeps the fixed initial state.
    res = initial_state.validate_layout(
        "epidemic", "abm",
        [{"x": 10.0 * i, "y": 50.0, "state": "infected" if i < 2 else "susceptible"}
         for i in range(12)],
        {"width": 300, "height": 300, "n": 12})
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"width": 300, "height": 300},
                         initial_state=res.layout)
    assert not models.validate_scene(scene)
    meta = manager.create_run(scene, seed=7, snapshot_interval=1)
    rid = meta["id"]
    try:
        assert meta["has_initial_state"]
        s0 = manager.get_snapshot(rid, 0)["individuals"]
        assert s0 == res.layout
        manager.reset(rid)
        assert manager.get_snapshot(rid, 0)["individuals"] == res.layout
    finally:
        manager.delete_run(rid)


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("initial-state import validation", initial_state_import)
    check("initial-state deterministic preview/run/reset", initial_state_determinism)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
