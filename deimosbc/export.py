"""Exports: GeoJSON for GIS tools, ProteusData-style edge CSV, PNG plot."""

from __future__ import annotations

import csv
import json
from typing import Any, Dict, Optional

from .geo import LocalFrame


def map_to_geojson(snapshot: Dict[str, Any], frame: LocalFrame, properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    pos = {v["id"]: frame.to_latlon(v["x"], v["y"]) for v in snapshot["vertices"]}
    feats = []
    for e in snapshot["edges"]:
        (la, lo), (lb, lob) = pos[e["u"]], pos[e["v"]]
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[round(lo, 7), round(la, 7)], [round(lob, 7), round(lb, 7)]]},
            "properties": {"kind": "path", "support": e["support"], "local_map": e["local_map"]},
        })
    for v in snapshot["vertices"]:
        if v.get("sensors"):
            la, lo = pos[v["id"]]
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lo, 7), round(la, 7)]},
                "properties": {"kind": "vertex", "id": v["id"], **v["sensors"]},
            })
    for h in snapshot.get("hazards", []):
        la, lo = frame.to_latlon(h["x"], h["y"])
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lo, 7), round(la, 7)]},
            "properties": {"kind": "hazard", "hazard": h["kind"]},
        })
    return {"type": "FeatureCollection", "properties": properties or {}, "features": feats}


def ground_truth_geojson(road, frame: LocalFrame) -> Dict[str, Any]:
    feats = []
    for ei, (u, v) in enumerate(road.edges):
        (la, lo), (lb, lob) = frame.to_latlon(*road.nodes[u]), frame.to_latlon(*road.nodes[v])
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[round(lo, 7), round(la, 7)], [round(lob, 7), round(lb, 7)]]},
            "properties": {"blocked": ei in road.blocked},
        })
    return {"type": "FeatureCollection", "features": feats}


def map_to_proteus_csv(snapshot: Dict[str, Any], frame: LocalFrame, path: str) -> int:
    """Write edges as ``x0,y0,x1,y1`` (lon/lat), the ProteusData format."""
    pos = {v["id"]: frame.to_latlon(v["x"], v["y"]) for v in snapshot["vertices"]}
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["x0", "y0", "x1", "y1"])
        for e in snapshot["edges"]:
            (la, lo), (lb, lob) = pos[e["u"]], pos[e["v"]]
            w.writerow([f"{lo:.6f}", f"{la:.6f}", f"{lob:.6f}", f"{lb:.6f}"])
    return len(snapshot["edges"])


def write_json(obj: Any, path: str) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=False, default=str)


def plot_run(sim, snapshot: Dict[str, Any], path: str, title: str = "") -> bool:
    """Ground truth (grey = open, red = blocked), mapped edges (blue),
    hazards (orange x), processors (black triangles). Needs matplotlib."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    road = sim.road
    fig, ax = plt.subplots(figsize=(8, 8), dpi=120)
    for ei, (u, v) in enumerate(road.edges):
        (x0, y0), (x1, y1) = road.nodes[u], road.nodes[v]
        blocked = ei in road.blocked
        ax.plot([x0, x1], [y0, y1], color="#d62728" if blocked else "#c7c7c7", lw=2.2 if blocked else 3.0,
                alpha=0.55 if blocked else 1.0, zorder=1, solid_capstyle="round")
    for z in road.floods:
        ax.add_patch(plt.Circle((z.x, z.y), z.radius, color="#1f77b4", alpha=0.07, zorder=0))
    pos = {v["id"]: (v["x"], v["y"]) for v in snapshot["vertices"]}
    for e in snapshot["edges"]:
        (x0, y0), (x1, y1) = pos[e["u"]], pos[e["v"]]
        ax.plot([x0, x1], [y0, y1], color="#1f4e9c", lw=1.0, zorder=2)
    hz = snapshot.get("hazards", [])
    if hz:
        ax.scatter([h["x"] for h in hz], [h["y"] for h in hz], marker="x", color="#ff7f0e", s=28, zorder=3, label="hazard report")
    ax.scatter([p.position[0] for p in sim.processors], [p.position[1] for p in sim.processors], marker="^", color="black",
               s=70, zorder=4, label="data processor")
    for p in sim.processors:
        ax.add_patch(plt.Circle(p.position, sim.cfg.collector_range_m, fill=False, ls=":", color="black", alpha=0.4))
    ax.plot([], [], color="#c7c7c7", lw=3, label="open street (truth)")
    ax.plot([], [], color="#d62728", lw=2, label="blocked street (truth)")
    ax.plot([], [], color="#1f4e9c", lw=1, label="released map")
    ax.set_aspect("equal")
    ax.set_xlim(0, sim.params.area_width_m)
    ax.set_ylim(0, sim.params.area_height_m)
    ax.set_xlabel("metres east")
    ax.set_ylabel("metres north")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=7, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return True
