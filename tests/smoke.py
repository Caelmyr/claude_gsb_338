"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import copy
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


# --------------------------------------------------------------------------- #
# Initial-state import
# --------------------------------------------------------------------------- #
def import_validation() -> None:
    tcfg = {"lanes": 2, "length": 10, "vmax": 5, "density": 0.2}

    ok = initial_state.validate_import(
        "traffic", "ca", tcfg,
        '[{"x":1,"y":0},{"x":2,"y":1,"v":2,"state":"moving"}]', "json")
    assert ok["ok"], ok["errors"]
    assert ok["individuals"][0]["v"] == 0
    assert ok["individuals"][1]["state"] == "moving"
    ids = [a["id"] for a in ok["individuals"]]
    assert len(set(ids)) == 2, ids

    # duplicate cell occupancy -> names both records
    dup = initial_state.validate_import(
        "traffic", "ca", tcfg, '[{"x":1,"y":0},{"x":1,"y":0}]', "json")
    assert not dup["ok"]
    assert any("重复占用同一格" in e["message"] for e in dup["errors"]), dup["errors"]

    # off-road lane -> bounds error naming the record
    off = initial_state.validate_import(
        "traffic", "ca", tcfg, '[{"x":1,"y":2}]', "json")
    assert not off["ok"]
    assert any("越界" in e["message"] and "第 1 条" in e["message"]
               for e in off["errors"]), off["errors"]

    # infected count exceeding the configured population
    over = initial_state.validate_import(
        "epidemic", "abm", {"n": 3, "width": 100, "height": 100},
        "x,y,state\n1,2,infected\n3,4,infected\n5,6,infected\n7,8,infected\n",
        "csv")
    assert not over["ok"]
    assert any("超过配置的人口规模" in e["message"] for e in over["errors"]), over["errors"]

    # illegal enum value
    enum = initial_state.validate_import(
        "epidemic", "ca", {"width": 5, "height": 5},
        '[{"x":1,"y":1,"state":"zombie"}]', "json")
    assert not enum["ok"]
    assert any("非法" in e["message"] for e in enum["errors"]), enum["errors"]

    # CSV with type-dependent defaults (energy = repro threshold / 2)
    csv_ok = initial_state.validate_import(
        "ecology", "ca",
        {"width": 10, "height": 10, "rabbit_repro": 8, "fox_repro": 12},
        "type,x,y\nrabbit,1,1\nfox,2,2\n", "csv")
    assert csv_ok["ok"], csv_ok["errors"]
    assert csv_ok["individuals"][0]["energy"] == 4
    assert csv_ok["individuals"][1]["energy"] == 6

    # traffic ABM: overlapping vehicles on the ring
    overlap = initial_state.validate_import(
        "traffic", "abm", {"road_length": 100.0, "length": 5.0, "v0": 30.0},
        "x,v\n10,0\n12,0\n", "csv")
    assert not overlap["ok"]
    assert any("重叠" in e["message"] for e in overlap["errors"]), overlap["errors"]

    # JSON wrapped in an object + unknown fields -> warning, still valid
    wrapped = initial_state.validate_import(
        "epidemic", "ca", {"width": 5, "height": 5},
        '{"individuals": [{"x":1,"y":1,"state":"infected","note":"x"}]}', "auto")
    assert wrapped["ok"], wrapped["errors"]
    assert any("未知字段" in w for w in wrapped["warnings"]), wrapped["warnings"]


def import_determinism() -> None:
    """Same import + same seed -> identical step-0 state, every time."""
    cfg = {"lanes": 2, "length": 10, "vmax": 5, "p_slow": 0.15,
           "density": 0.2, "lane_change": True}
    res = initial_state.validate_import(
        "traffic", "ca", cfg, '[{"x":1,"y":0,"v":1},{"x":5,"y":1}]', "json")
    assert res["ok"], res["errors"]
    full = {**cfg, "initial_state": res["individuals"]}
    a = make_engine("traffic", "ca", config=full, seed=7)
    b = make_engine("traffic", "ca", config=full, seed=7)
    assert a.snapshot() == b.snapshot()
    assert [v["x"] for v in a.individuals()] == [1, 5]
    # the engine must not mutate the stored import
    a.step()
    assert res["individuals"][0]["x"] == 1


def import_preview_matches_run() -> None:
    """Preview snapshot (validate endpoint path) == run step-0 snapshot."""
    scene_cfg = models.resolve_config(models.Scene(
        domain="ecology", model="ca", config={"width": 20, "height": 20}))
    text = '[{"type":"rabbit","x":1,"y":1},{"type":"rabbit","x":2,"y":2},' \
           '{"type":"fox","x":5,"y":5}]'
    res = initial_state.validate_import("ecology", "ca", scene_cfg, text, "json")
    assert res["ok"], res["errors"]

    # preview path: engine built from the normalized import
    preview = make_engine("ecology", "ca",
                          config={**scene_cfg,
                                  "initial_state": res["individuals"]},
                          seed=0)
    # runtime path: scene saved with the import, run created from it
    scene = models.Scene(domain="ecology", model="ca",
                         config={**scene_cfg,
                                 "initial_state": res["individuals"]})
    assert not models.validate_scene(scene), models.validate_scene(scene)
    meta = manager.create_run(scene, snapshot_interval=1)
    rid = meta["id"]
    try:
        snap0 = manager.get_snapshot(rid, 0)
        assert snap0["individuals"] == preview.snapshot()["individuals"]
        assert snap0["stats"] == preview.stats()
        # reset reproduces the exact imported layout again (deep-copy first:
        # engine snapshots share live dicts that later steps mutate in place)
        snap0 = copy.deepcopy(snap0)
        manager.step(rid, 3)
        manager.reset(rid)
        assert manager.get_snapshot(rid, 0)["individuals"] == snap0["individuals"]
    finally:
        manager.delete_run(rid)


def import_scene_revalidation() -> None:
    """A stored import that no longer fits the config blocks saving."""
    scene = models.Scene(
        domain="traffic", model="ca",
        config={"lanes": 1, "length": 20,
                "initial_state": [{"id": "a", "type": "vehicle",
                                   "state": "stopped", "x": 25, "y": 0, "v": 0}]})
    errors = models.validate_scene(scene)
    assert any("初始态" in e and "越界" in e for e in errors), errors

    # and a consistent one passes
    scene.config["initial_state"][0]["x"] = 3
    assert models.validate_scene(scene) == []


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("initial-state import validation", import_validation)
    check("initial-state determinism", import_determinism)
    check("initial-state preview == run", import_preview_matches_run)
    check("initial-state scene revalidation", import_scene_revalidation)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
