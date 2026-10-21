"""Regenerable artifacts derived from the spatial model, managed through a declarative registry."""

from cad2anything.generators.registry import (
    ArtifactRecord,
    GeneratorContext,
    GeneratorResult,
    GeneratorSpec,
    ImpactEntry,
    Issue,
    approve,
    load_specs,
    plan_impact,
    reject,
    run_generator,
)

__all__ = [
    "ArtifactRecord",
    "GeneratorContext",
    "GeneratorResult",
    "GeneratorSpec",
    "ImpactEntry",
    "Issue",
    "approve",
    "load_specs",
    "plan_impact",
    "reject",
    "run_generator",
]
