"""R2.4 analyzer adapters — external security scanners as evidence producers.

DESIGN_v2.md §6.1: "Add adapters for OpenGrep/Semgrep, ast-grep, OSV-Scanner,
Trivy, gitleaks, actionlint, zizmor, Checkov, Bandit"; §16 package layout puts
them in ``engine/analyzers/`` (``semgrep.py osv.py trivy.py gitleaks.py ...``).

Each module pairs a pure parser (``parse_*_json`` — testable with canned JSON)
with a runner (``run_*``) that executes the tool when installed and returns
kind='static_analysis' Evidence. All runners degrade gracefully to ``[]``
(with a logged note) when the binary is absent.
"""

from engine.analyzers.astgrep import parse_astgrep_json, run_astgrep
from engine.analyzers.gitleaks import parse_gitleaks_json, run_gitleaks
from engine.analyzers.semgrep import parse_semgrep_json, run_semgrep
from engine.analyzers.trivy import parse_trivy_json, run_trivy

__all__ = [
    "run_semgrep",
    "run_astgrep",
    "run_trivy",
    "run_gitleaks",
    "parse_semgrep_json",
    "parse_astgrep_json",
    "parse_trivy_json",
    "parse_gitleaks_json",
]
