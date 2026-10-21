"""Layout planning: transparent what-if scoring, constraint-aware alternatives, hypothetical revisions.

All expected sales and deltas are estimates (추정치) from ``whatif.ScoringModel``.
"""

from cad2anything.layout.engine import (
    LayoutAlternative,
    LayoutConstraints,
    Move,
    check_constraints,
    generate_alternatives,
)
from cad2anything.layout.hypothetical import to_hypothetical_revision
from cad2anything.layout.whatif import (
    AssignmentScore,
    AssignmentScorer,
    ScoringModel,
    assignment_from_features,
    fit_scoring_model,
    normalize_assignment,
    score_assignment,
)

__all__ = [
    "AssignmentScore",
    "AssignmentScorer",
    "LayoutAlternative",
    "LayoutConstraints",
    "Move",
    "ScoringModel",
    "assignment_from_features",
    "check_constraints",
    "fit_scoring_model",
    "generate_alternatives",
    "normalize_assignment",
    "score_assignment",
    "to_hypothetical_revision",
]
