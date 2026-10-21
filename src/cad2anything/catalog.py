"""Domain catalog for the synthetic flagship department store.

All brand names are fictional and generated elsewhere. Numbers here are synthetic calibration
targets informed by public aggregate statistics (see ``docs/data``); they are not real store data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FACILITY_CODE = "FLAGSHIP"
FACILITY_NAME = "가상 플래그십점"
BUILDING_WIDTH_M = 120.0
BUILDING_DEPTH_M = 80.0
FLOOR_HEIGHT_M = 5.0


@dataclass(frozen=True)
class Category:
    code: str
    name_ko: str
    mean_ticket_krw: int
    commission_range: tuple[float, float]
    space_beta: float
    unit_area_range_m2: tuple[float, float]
    fixture_types: tuple[str, ...]
    is_food_beverage: bool = False


CATEGORIES: dict[str, Category] = {
    c.code: c
    for c in [
        Category("LUXURY", "명품", 850_000, (0.10, 0.20), 0.18, (250, 450), ("SHOWCASE", "TABLE", "MANNEQUIN", "COUNTER")),
        Category("JEWELRY_WATCH", "주얼리·시계", 600_000, (0.15, 0.25), 0.15, (60, 120), ("SHOWCASE", "COUNTER")),
        Category("COSMETICS", "화장품", 95_000, (0.25, 0.35), 0.35, (40, 90), ("SHOWCASE", "COUNTER", "TABLE")),
        Category("GLOBAL_FASHION", "해외패션", 380_000, (0.20, 0.30), 0.20, (120, 250), ("HANGER", "TABLE", "MANNEQUIN", "FITTING", "COUNTER")),
        Category("ACCESSORIES", "잡화", 180_000, (0.28, 0.36), 0.08, (50, 110), ("SHOWCASE", "TABLE", "COUNTER")),
        Category("WOMEN_CONTEMPORARY", "여성 컨템포러리", 210_000, (0.30, 0.38), 0.22, (80, 180), ("HANGER", "TABLE", "MANNEQUIN", "FITTING", "COUNTER")),
        Category("WOMEN_CASUAL", "여성 캐주얼", 120_000, (0.30, 0.38), 0.20, (80, 160), ("HANGER", "TABLE", "FITTING", "COUNTER")),
        Category("LINGERIE", "란제리", 70_000, (0.30, 0.38), 0.15, (50, 90), ("HANGER", "TABLE", "FITTING", "COUNTER")),
        Category("MEN_FORMAL", "남성 정장", 260_000, (0.30, 0.36), 0.18, (90, 200), ("HANGER", "TABLE", "MANNEQUIN", "FITTING", "COUNTER")),
        Category("MEN_CASUAL", "남성 캐주얼", 140_000, (0.30, 0.36), 0.20, (90, 180), ("HANGER", "TABLE", "FITTING", "COUNTER")),
        Category("SPORTS", "스포츠", 130_000, (0.25, 0.33), 0.22, (120, 300), ("HANGER", "GONDOLA", "TABLE", "FITTING", "COUNTER")),
        Category("OUTDOOR", "아웃도어", 190_000, (0.25, 0.33), 0.20, (120, 250), ("HANGER", "GONDOLA", "TABLE", "FITTING", "COUNTER")),
        Category("KIDS", "아동", 85_000, (0.25, 0.33), 0.20, (70, 150), ("HANGER", "GONDOLA", "TABLE", "COUNTER")),
        Category("LIVING", "리빙", 160_000, (0.22, 0.30), 0.25, (120, 300), ("GONDOLA", "TABLE", "SHOWCASE", "COUNTER")),
        Category("ELECTRONICS", "가전", 420_000, (0.08, 0.15), 0.18, (150, 350), ("GONDOLA", "TABLE", "COUNTER")),
        Category("FOOD_FRESH", "신선식품", 38_000, (0.15, 0.22), 0.20, (40, 90), ("SHOWCASE", "COUNTER"), True),
        Category("FOOD_DELI", "델리", 24_000, (0.18, 0.25), 0.20, (30, 70), ("SHOWCASE", "COUNTER"), True),
        Category("BAKERY", "베이커리", 15_000, (0.18, 0.25), 0.20, (30, 70), ("SHOWCASE", "COUNTER"), True),
        Category("WINE", "와인·주류", 70_000, (0.15, 0.22), 0.15, (40, 90), ("GONDOLA", "SHOWCASE", "COUNTER"), True),
        Category("FOOD_COURT", "푸드코트", 14_000, (0.12, 0.18), 0.10, (20, 40), ("COUNTER", "DINING"), True),
        Category("CAFE", "카페", 11_000, (0.12, 0.18), 0.10, (60, 150), ("COUNTER", "DINING"), True),
        Category("RESTAURANT", "레스토랑", 45_000, (0.10, 0.15), 0.12, (120, 250), ("COUNTER", "DINING"), True),
        Category("CULTURE", "문화홀", 30_000, (0.05, 0.10), 0.05, (400, 800), ("TABLE", "COUNTER")),
    ]
}


@dataclass(frozen=True)
class FloorProgram:
    code: str
    ordinal: int
    name_ko: str
    category_mix: dict[str, float] = field(default_factory=dict)


FLOOR_PROGRAMS: list[FloorProgram] = [
    FloorProgram("B1", -1, "식품관", {"FOOD_FRESH": 0.30, "FOOD_DELI": 0.25, "BAKERY": 0.15, "WINE": 0.10, "FOOD_COURT": 0.15, "CAFE": 0.05}),
    FloorProgram("1F", 0, "명품·화장품", {"LUXURY": 0.30, "COSMETICS": 0.55, "JEWELRY_WATCH": 0.15}),
    FloorProgram("2F", 1, "해외패션·잡화", {"GLOBAL_FASHION": 0.55, "ACCESSORIES": 0.30, "LUXURY": 0.15}),
    FloorProgram("3F", 2, "여성 컨템포러리", {"WOMEN_CONTEMPORARY": 0.85, "CAFE": 0.05, "ACCESSORIES": 0.10}),
    FloorProgram("4F", 3, "여성 캐주얼", {"WOMEN_CASUAL": 0.70, "LINGERIE": 0.20, "ACCESSORIES": 0.10}),
    FloorProgram("5F", 4, "남성", {"MEN_FORMAL": 0.45, "MEN_CASUAL": 0.45, "CAFE": 0.10}),
    FloorProgram("6F", 5, "스포츠·아동", {"SPORTS": 0.40, "OUTDOOR": 0.30, "KIDS": 0.30}),
    FloorProgram("7F", 6, "리빙", {"LIVING": 0.75, "ELECTRONICS": 0.25}),
    FloorProgram("8F", 7, "식당가·문화홀", {"RESTAURANT": 0.70, "CAFE": 0.15, "CULTURE": 0.15}),
]

FLOOR_CODES: list[str] = [f.code for f in FLOOR_PROGRAMS]


@dataclass(frozen=True)
class FixtureType:
    code: str
    name_ko: str
    width_m: float
    depth_m: float


FIXTURE_TYPES: dict[str, FixtureType] = {
    f.code: f
    for f in [
        FixtureType("SHOWCASE", "쇼케이스", 1.8, 0.6),
        FixtureType("HANGER", "행거", 1.2, 0.5),
        FixtureType("GONDOLA", "곤돌라", 1.2, 0.9),
        FixtureType("COUNTER", "카운터", 2.0, 0.8),
        FixtureType("TABLE", "진열테이블", 1.6, 1.0),
        FixtureType("MANNEQUIN", "마네킹스테이지", 1.0, 1.0),
        FixtureType("FITTING", "피팅룸", 1.5, 1.5),
        FixtureType("DINING", "식탁", 1.2, 0.8),
    ]
}

AMENITY_TYPES: dict[str, str] = {
    "RESTROOM": "화장실",
    "INFO": "안내데스크",
    "ATM": "ATM",
    "NURSING": "수유실",
    "LOUNGE": "고객라운지",
}

VERTICAL_LINKS: dict[str, str] = {
    "ESC-A": "escalator",
    "ESC-B": "escalator",
    "EV-1": "elevator",
    "EV-2": "elevator",
    "ST-1": "stair",
    "ST-2": "stair",
}

AGE_BANDS: tuple[str, ...] = ("20s", "30s", "40s", "50s", "60s+")
GENDERS: tuple[str, ...] = ("F", "M")
TIERS: tuple[str, ...] = ("VVIP", "VIP", "GOLD", "MEMBER")


def floor_program(code: str) -> FloorProgram:
    for program in FLOOR_PROGRAMS:
        if program.code == code:
            return program
    raise KeyError(code)
