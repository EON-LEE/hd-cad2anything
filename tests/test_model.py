from shapely.geometry import Polygon

from cad2anything.catalog import CATEGORIES, FLOOR_CODES, FLOOR_PROGRAMS
from cad2anything.model import (
    ChangeItem,
    ChangeSet,
    ChangeType,
    Entity,
    EntityKind,
    Floor,
    Relation,
    RelationType,
    Revision,
    SpatialModel,
)


def test_spatial_model_round_trip():
    model = SpatialModel(revision=Revision("R0", effective_date="2024-10-01"), floors=[Floor("3F", 2)])
    model.add(
        Entity(
            "U-3F-N05",
            EntityKind.UNIT,
            "3F",
            Polygon([(0, 0), (10, 0), (10, 8), (0, 8)]),
            label="3F-N05",
            attributes={"unit_code": "3F-N05", "brand_id": "BR0001"},
        )
    )
    model.relations.append(Relation("F-3F", "U-3F-N05", RelationType.CONTAINS))

    restored = SpatialModel.from_dict(model.to_dict())

    assert restored.unit_by_code("3F-N05").geometry.area == 80
    assert restored.relations[0].type == RelationType.CONTAINS
    assert restored.revision.effective_date == "2024-10-01"


def test_change_set_round_trip():
    change_set = ChangeSet("R0", "R1", [ChangeItem(ChangeType.MOVE, "3F", ["3F-N05"], ["3F-C03"])])

    restored = ChangeSet.from_dict(change_set.to_dict())

    assert restored.items[0].change_type == ChangeType.MOVE
    assert restored.items[0].unit_codes_after == ["3F-C03"]


def test_catalog_floor_programs_reference_known_categories():
    assert FLOOR_CODES[0] == "B1" and FLOOR_CODES[-1] == "8F"
    for program in FLOOR_PROGRAMS:
        assert abs(sum(program.category_mix.values()) - 1.0) < 1e-9
        assert set(program.category_mix) <= set(CATEGORIES)
