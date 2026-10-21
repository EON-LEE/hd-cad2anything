from pathlib import Path
from shutil import copytree

import pytest

from cad2anything.mockgen.business import generate_business
from cad2anything.mockgen.drawings import build_all
from cad2anything.workspace import Workspace

SEED = 42
SMALL_SCALE = 0.002


@pytest.fixture(scope="session")
def synthetic_store(tmp_path_factory):
    """Ground-truth models and change sets plus small-scale business tables (seed 42)."""
    brands, models, changes = build_all(SEED)
    out = tmp_path_factory.mktemp("synthetic-store")
    generate_business(out, seed=SEED, scale=SMALL_SCALE, models=models, brands=brands)
    return {"brands": brands, "models": models, "changes": changes, "business_dir": out / "business"}


def populate_workspace(root: Path, store: dict, business: bool = True) -> Workspace:
    workspace = Workspace(root)
    for model in store["models"].values():
        workspace.save_model(model, {"source": "truth"})
    for change_set in store["changes"].values():
        workspace.save_change_set(change_set, {"source": "truth"})
    if business:
        copytree(store["business_dir"], root / "business", dirs_exist_ok=True)
    return workspace


@pytest.fixture(scope="session")
def populate(synthetic_store):
    """Return a function that fills a workspace root with the synthetic store."""
    return lambda root, business=True: populate_workspace(root, synthetic_store, business)
