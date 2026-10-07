"""Headless command-line batch runner.

Run a scene from the terminal without the web UI — useful for quick parameter
sweeps and for verifying the simulation + storage layer independently:

    python3 cli.py --list
    python3 cli.py --scene scene_epidemic_abm --steps 200 --report
    python3 cli.py --scene scene_traffic_ca --steps 500 --snapshot-interval 5
"""

from __future__ import annotations

import argparse
import os
import sys

from backend import initial_state, models, report, run_manager, storage, util


def list_scenes() -> None:
    for s in storage.list_scenes():
        print(f"{s['id']:24s} {s['domain']:8s}/{s['model']:3s}  {s['name']}")


def _load_layout_file(path: str, domain: str, model: str,
                      config: dict):
    """Parse + validate a JSON/CSV initial-state file against the scene."""
    fmt = "json" if os.path.splitext(path)[1].lower() == ".json" else "csv"
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    parsed = initial_state.parse_layout_text(text, fmt)
    if not parsed.ok:
        raise SystemExit(f"初始态文件解析失败：{parsed.error}")
    result = initial_state.validate_layout(domain, model, parsed.rows, config)
    if not result.ok:
        print("初始态校验失败：", file=sys.stderr)
        for e in result.errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(2)
    return result


def run_one(scene_id: str, steps: int, snapshot_interval: int,
            make_report: bool, initial_state_file: str = "") -> int:
    scene = storage.load_scene(scene_id)
    if scene is None:
        print(f"error: scene not found: {scene_id}", file=sys.stderr)
        return 1
    scene_obj = models.Scene.from_dict(scene)
    if initial_state_file:
        # CLI override: validate against the merged config, then attach.
        cfg = models.resolve_config(scene_obj)
        result = _load_layout_file(initial_state_file,
                                   scene_obj.domain, scene_obj.model, cfg)
        scene_obj.initial_state = result.layout
        scene_obj.config.update(result.config_updates)
        for w in result.warnings:
            print(f"note: {w}")
        print(f"initial state: {len(result.layout)} individuals from "
              f"{initial_state_file}")
    meta = run_manager.manager.create_run(
        scene_obj, snapshot_interval=snapshot_interval)
    print(f"run {meta['id']}: {meta['name']} "
          f"({meta['domain']}/{meta['model']}) seed={meta['seed']}"
          f"{' [deterministic initial state]' if meta.get('has_initial_state') else ''}")

    result = run_manager.manager.run_batch(meta["id"], steps, keep_engine=True)
    print(f"finished at step {result['step']}")
    for k, v in result["stats"].items():
        print(f"  {k:16s} {v}")

    if make_report:
        rpt = report.generate_report(meta["id"])
        print("\nsummary:")
        for line in rpt["summary"]:
            print(f"  - {line}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Headless simulation batch runner")
    p.add_argument("--list", action="store_true", help="list available scenes")
    p.add_argument("--scene", help="scene id to run")
    p.add_argument("--steps", type=int, default=200, help="steps to run")
    p.add_argument("--snapshot-interval", type=int, default=1,
                   help="persist a full snapshot every N steps")
    p.add_argument("--initial-state", default="",
                   help="import a deterministic initial state from JSON/CSV "
                        "(validated row by row; overrides random seeding)")
    p.add_argument("--report", action="store_true", help="generate a report")
    args = p.parse_args()

    storage.ensure_dirs()
    if args.list:
        list_scenes()
        return 0
    if not args.scene:
        p.print_help()
        return 1
    return run_one(args.scene, args.steps, args.snapshot_interval,
                   args.report, args.initial_state)


if __name__ == "__main__":
    sys.exit(main())
