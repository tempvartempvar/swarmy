"""Filesystem locations. The package is meant to be used from a checkout (pip install -e .)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "configs"
DEFAULT_MODELS_FILE = CONFIG_DIR / "models.yaml"
CACHE_DIR = REPO_ROOT / ".cache"
DEFAULT_RESULTS_DIR = Path("results")
