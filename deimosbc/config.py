"""Protocol parameters.

Everything in :class:`ProtocolParams` is written into the genesis block, so all
processors execute the smart contracts with identical rules. Simulation-only
knobs live in :mod:`deimosbc.sim.scenario`.

Where the paper leaves a value open, the default chosen here is documented in
``docs/DESIGN.md`` under "Interpretation decisions".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict


@dataclass
class ProtocolParams:
    # --- Area of operation -------------------------------------------------
    origin_lat: float = 29.7520            # Houston, TX (Gulf Coast scenario)
    origin_lon: float = -95.3700
    area_width_m: float = 2000.0           # x-extent of the disaster area
    area_height_m: float = 2000.0          # y-extent of the disaster area
    task_cell_size_m: float = 250.0        # Task Assignment granularity

    # --- Trace Management System (Sec. IV-B.2) -----------------------------
    sample_displacement_m: float = 10.0    # "separated by a thresh-holding distance"
    segmentation_angle_deg: float = 60.0   # trajectory segmentation on abrupt turns
    segmentation_gap_m: float = 60.0       # split trace on GPS gaps
    min_trace_points: int = 3
    max_trace_points: int = 120

    # --- Upload validation rules in the map contract (Sec. IV-C) -----------
    min_point_spacing_m: float = 2.0
    max_point_gap_m: float = 80.0
    max_speed_mps: float = 15.0
    task_overlap_tolerance_m: float = 100.0
    max_sensor_fields: int = 8

    # --- Data Summarization System (Sec. IV-B.1) ---------------------------
    merger: str = "overlap-chain"          # key in deimosbc.mapping.merger.MERGERS
    merge_radius_m: float = 15.0           # overlap = within this distance
    max_angle_deviation_deg: float = 35.0  # co-located segments must agree in heading
    conflict_min_run_m: float = 40.0       # co-located misaligned run length => conflict
    hazard_radius_m: float = 15.0          # hazard report vs. traversed path conflict

    # --- Extensions beyond the 2021 paper (defaults = paper behaviour) -----
    # "discard_both": paper rule, every conflicting trace is discarded.
    # "evidence":     hazard conflicts are settled by the number of distinct
    #                 collectors on each side; ties keep the hazard (safety
    #                 first). Geometric conflicts still discard both.
    conflict_policy: str = "discard_both"
    # Released maps only contain edges traversed by at least this many
    # distinct collectors (1 = paper behaviour, no corroboration).
    min_release_support: int = 1

    # --- Incentives (Sec. IV-C, "Upload Sensory Data") ---------------------
    initial_collector_balance: int = 100
    submission_stake: int = 10             # forfeited if data is unqualified
    reward_per_trace: int = 2
    reward_per_new_metre: float = 0.02

    # --- Collector error handling (Sec. IV-A.1) ----------------------------
    max_errors_before_reauth: int = 3
    max_reauths: int = 2                   # after this many, the collector is disabled

    # --- Tasks (Sec. IV-C, "Task Assignment") ------------------------------
    task_complete_length_m: float = 350.0  # new map length inside a cell => "mapped"
    claim_timeout_blocks: int = 120
    max_claims_per_task: int = 1

    # --- Consensus / governance (Sec. IV-C, "Retirement") ------------------
    block_interval_ticks: int = 5
    max_txs_per_block: int = 200
    integrity_window: int = 40             # recent submissions examined
    integrity_min_samples: int = 20
    freeze_error_rate: float = 0.6         # error share that triggers a freeze proposal
    release_interval_blocks: int = 24      # periodic snapshot for requesters
    release_quiet_blocks: int = 12         # "no more updates are required"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProtocolParams":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    @property
    def n_cells_x(self) -> int:
        import math

        return max(1, math.ceil(self.area_width_m / self.task_cell_size_m))

    @property
    def n_cells_y(self) -> int:
        import math

        return max(1, math.ceil(self.area_height_m / self.task_cell_size_m))
