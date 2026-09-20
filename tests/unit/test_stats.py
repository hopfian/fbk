"""stats.py — the shared pure-statistics helper (docs/10 §8) + the
journal.analysis import-edge contract.

percentile moved here from surfaces/measurement.py (it has no surface
semantics); the burst/sparse pacing thresholds moved to constants.py.
journal.analysis imports both from the new homes, so importing the
audit must no longer pull the surfaces package at all — that weight
win is pinned here as a fresh-interpreter subprocess check, immune to
whatever earlier tests loaded into this process's sys.modules.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stats import percentile

CLI_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = CLI_ROOT / "src"


# ------------------------------------------------------------------ percentile
class TestPercentile:
    """Pins the nearest-rank math: rank = ceil(pct/100 * n), clamped to n,
    and the empty-window 0.0 contract (docs/10 §8 report columns)."""

    def test_nearest_rank_math(self) -> None:
        values = [4.0, 8.0, 12.0, 16.0, 20.0]  # already sorted
        # rank ceil(0.5*5)=3 -> 12.0; rank ceil(0.95*5)=5 -> 20.0
        assert percentile(values, 50.0) == 12.0
        assert percentile(values, 95.0) == 20.0

    def test_extreme_percentiles_clamp(self) -> None:
        values = [1.0, 2.0, 3.0]
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 100.0) == 3.0

    def test_empty_list_yields_zero(self) -> None:
        assert percentile([], 95.0) == 0.0


# --------------------------------------------------- the journal.analysis weight
class TestJournalAnalysisImportEdge:
    """Fresh-interpreter proof that journal.analysis no longer touches the
    surfaces package: before the stats/constants extraction it imported
    surfaces.measurement, which initializes the whole surfaces barrel
    (feed, messenger, marketplace, ...) as a side effect."""

    @staticmethod
    def _fresh_interpreter_modules(stmts: str) -> list[str]:
        code = (
            "import sys; "
            + stmts
            + "; import json; "
            "print(json.dumps(sorted(sys.modules)))"
        )
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
            capture_output=True, text=True, check=True)
        import json
        return json.loads(proc.stdout)

    def test_analysis_import_pulls_no_surfaces_modules(self) -> None:
        modules = self._fresh_interpreter_modules("import journal.analysis")
        assert not [m for m in modules if m.startswith("surfaces")]
        assert "journal.analysis" in modules  # the audit itself is loaded

    def test_all_entry_orders_import_clean(self) -> None:
        # each order in its own fresh interpreter: no partial-initialization
        # error can hide behind a module another order already finished
        for stmts in ("import journal", "import surfaces",
                      "import surfaces.measurement", "import journal.analysis",
                      "import journal; import surfaces",
                      "import surfaces; import journal"):
            subprocess.run(
                [sys.executable, "-X", "utf8", "-c", stmts],
                env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
                capture_output=True, text=True, check=True)

    def test_barrel_serves_the_analysis_exports_eagerly(self) -> None:
        import journal
        import journal.analysis
        from journal import PacingAudit, audit_pacing, request_gaps  # noqa: F401

        assert journal.audit_pacing is journal.analysis.audit_pacing
        assert journal.request_gaps is journal.analysis.request_gaps
        assert journal.PacingAudit is journal.analysis.PacingAudit
