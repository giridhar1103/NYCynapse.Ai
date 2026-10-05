"""Read the semantic YAML into a Catalog."""

from pathlib import Path

import yaml

from .schema import Catalog


def _read(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_catalog(root: Path) -> Catalog:
    head = _read(root / "workspaces.yaml")
    models, metrics = [], []
    for path in sorted((root / "models").glob("*.yaml")):
        models += _read(path).get("models", [])
    for path in sorted((root / "metrics").glob("*.yaml")):
        metrics += _read(path).get("metrics", [])
    return Catalog(
        version=str(head["version"]),
        workspaces=head["workspaces"],
        models=models,
        metrics=metrics,
        relationships=_read(root / "relationships.yaml").get("relationships", []),
        periods=_read(root / "time.yaml").get("periods", []),
        places=_read(root / "places.yaml").get("places", []),
        instructions=_read(root / "instructions.yaml").get("instructions", []),
        aliases=_read(root / "aliases.yaml").get("aliases", [])
        if (root / "aliases.yaml").exists()
        else [],
    )
