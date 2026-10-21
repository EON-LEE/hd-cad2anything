"""Deterministic catalog of fictional brands.

Brand names are assembled from invented morphemes (English-style stems plus endings) or Korean word
pools plus category suffixes. A blocklist of well-known real brand names is enforced, but it is stored
only as truncated SHA-256 digests of normalized names so that this public repository never spells out
any real brand.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cad2anything.catalog import CATEGORIES, FLOOR_PROGRAMS

NOMINAL_TENANT_AREA_M2 = 5472.0
SPARE_FACTOR = 1.5
MIN_SPARES = 5

_EN_STEMS = (
    "Alv", "Arn", "Bel", "Bren", "Cael", "Cor", "Dar", "Dov", "Elar", "Eph", "Fal", "Fenn", "Gav", "Gior",
    "Hael", "Hol", "Ilv", "Isk", "Jor", "Jun", "Kael", "Kor", "Lir", "Lum", "Mav", "Mer", "Ner", "Nov",
    "Orl", "Oss", "Pav", "Pell", "Quor", "Rav", "Rel", "Sev", "Sol", "Tal", "Tov", "Ulm", "Vael", "Ves",
    "Wyn", "Xer", "Yor", "Zel", "Brav", "Cass", "Dren", "Esk", "Fior", "Grel", "Lorv", "Mirr", "Nyss",
)  # fmt: skip
_EN_ENDINGS = (
    "ane", "ara", "elle", "ena", "ette", "ia", "ier", "ine", "io", "is", "ix", "on", "ora", "ova", "ue",
    "ys", "en", "ard", "ent", "eth", "andre", "esse", "ino", "uro",
)  # fmt: skip
_KO_STEMS = (
    "가온", "나린", "다솜", "라온", "마루", "새솔", "소담", "아라", "온새", "윤슬", "하람", "해솔", "한결",
    "누리", "여울", "가람", "나래", "다온", "로아", "별하", "시루", "모아", "보미", "채움", "그루", "도담",
    "미리내", "늘봄", "단비", "들꽃", "온담", "푸름", "이슬", "하랑", "바름", "솔빛", "고운", "참빛", "물길",
    "볕뜰", "구름", "새봄",
)  # fmt: skip

# "{E}" = English-style invented word, "{E2}" = second invented word, "{K}" = Korean word.
_TEMPLATES: dict[str, tuple[str, ...]] = {
    "LUXURY": ("{E}", "Maison {E}", "Casa {E}", "{E} & {E2}"),
    "JEWELRY_WATCH": ("{E} Jewels", "{E} Horology", "Atelier {E}"),
    "COSMETICS": ("{E} Beauty", "{E} Skin", "{E} Lab", "{E} Cosmetics", "{K}뷰티"),
    "GLOBAL_FASHION": ("{E}", "{E} Studio", "{E} {E2}"),
    "ACCESSORIES": ("{E} Bags", "{E} & Co", "{E} Leather"),
    "WOMEN_CONTEMPORARY": ("{E}", "Studio {E}", "{E} Femme"),
    "WOMEN_CASUAL": ("{E}", "{E} Daily", "{E} Weekend"),
    "LINGERIE": ("{E} Intimates", "{E} Lingerie", "{E} Nuit"),
    "MEN_FORMAL": ("{E} Tailoring", "{E} Homme", "{E} Sartoria"),
    "MEN_CASUAL": ("{E} Supply", "{E} Denim", "{E} Workshop"),
    "SPORTS": ("{E} Athletics", "{E} Sport", "{E} Active"),
    "OUTDOOR": ("{E} Outdoor", "{E} Trail", "{E} Alpine"),
    "KIDS": ("{E} Kids", "Little {E}", "{K}키즈"),
    "LIVING": ("{E} Home", "{E} Living", "{K}리빙"),
    "ELECTRONICS": ("{E} Electric", "{E} Tech", "{E} Audio"),
    "FOOD_FRESH": ("{K}농장", "{K}청과", "{K}수산", "{K}정육"),
    "FOOD_DELI": ("{K}반찬", "{K}델리", "{K}찬방"),
    "BAKERY": ("{K}제과", "{K}베이커리", "Boulangerie {E}"),
    "WINE": ("{E} Cellar", "{E} Vins", "{K}와인"),
    "FOOD_COURT": ("{K}국수", "{K}분식", "{K}덮밥", "{K}만두"),
    "CAFE": ("{E} Coffee", "{K}다방", "Cafe {E}", "{E} Tea House"),
    "RESTAURANT": ("{K}식당", "{K}옥", "Trattoria {E}", "{E} Bistro", "{K}한상"),
    "CULTURE": ("{E} 아트홀", "{K} 문화홀", "{E} 갤러리"),
}

ADJACENCY_LIKES: dict[str, tuple[str, ...]] = {
    "LUXURY": ("JEWELRY_WATCH", "GLOBAL_FASHION", "COSMETICS"),
    "JEWELRY_WATCH": ("LUXURY", "COSMETICS"),
    "COSMETICS": ("JEWELRY_WATCH", "ACCESSORIES", "LUXURY"),
    "GLOBAL_FASHION": ("LUXURY", "ACCESSORIES"),
    "ACCESSORIES": ("WOMEN_CONTEMPORARY", "COSMETICS", "GLOBAL_FASHION"),
    "WOMEN_CONTEMPORARY": ("ACCESSORIES", "CAFE", "WOMEN_CASUAL"),
    "WOMEN_CASUAL": ("LINGERIE", "ACCESSORIES", "WOMEN_CONTEMPORARY"),
    "LINGERIE": ("WOMEN_CASUAL", "ACCESSORIES"),
    "MEN_FORMAL": ("MEN_CASUAL", "ACCESSORIES"),
    "MEN_CASUAL": ("MEN_FORMAL", "SPORTS", "CAFE"),
    "SPORTS": ("OUTDOOR", "MEN_CASUAL"),
    "OUTDOOR": ("SPORTS",),
    "KIDS": ("CAFE", "LIVING"),
    "LIVING": ("ELECTRONICS", "KIDS"),
    "ELECTRONICS": ("LIVING",),
    "FOOD_FRESH": ("FOOD_DELI", "WINE"),
    "FOOD_DELI": ("FOOD_FRESH", "BAKERY", "FOOD_COURT"),
    "BAKERY": ("CAFE", "FOOD_DELI"),
    "WINE": ("FOOD_FRESH", "FOOD_DELI"),
    "FOOD_COURT": ("FOOD_DELI", "CAFE"),
    "CAFE": ("BAKERY", "WOMEN_CONTEMPORARY", "CULTURE"),
    "RESTAURANT": ("CAFE", "CULTURE"),
    "CULTURE": ("CAFE", "RESTAURANT"),
}

ADJACENCY_CONFLICTS: dict[str, tuple[str, ...]] = {
    "LUXURY": ("FOOD_COURT", "FOOD_DELI", "SPORTS"),
    "JEWELRY_WATCH": ("FOOD_COURT", "CAFE"),
    "COSMETICS": ("FOOD_COURT", "RESTAURANT"),
    "GLOBAL_FASHION": ("FOOD_COURT",),
    "ACCESSORIES": ("FOOD_COURT",),
    "WOMEN_CONTEMPORARY": ("FOOD_COURT", "MEN_FORMAL"),
    "WOMEN_CASUAL": ("FOOD_COURT",),
    "LINGERIE": ("MEN_FORMAL", "KIDS", "CAFE"),
    "MEN_FORMAL": ("LINGERIE",),
    "MEN_CASUAL": ("LINGERIE",),
    "SPORTS": ("LUXURY",),
    "OUTDOOR": ("LUXURY",),
    "KIDS": ("LINGERIE", "WINE"),
    "LIVING": ("FOOD_COURT",),
    "ELECTRONICS": ("CAFE",),
    "FOOD_FRESH": ("COSMETICS",),
    "FOOD_DELI": ("LUXURY",),
    "BAKERY": ("WINE",),
    "WINE": ("KIDS",),
    "FOOD_COURT": ("LUXURY", "JEWELRY_WATCH"),
    "CAFE": ("JEWELRY_WATCH", "LINGERIE"),
    "RESTAURANT": ("COSMETICS",),
    "CULTURE": ("FOOD_COURT",),
}

# Truncated SHA-256 digests of normalized well-known brand and retailer names (see module docstring).
_BLOCKED_DIGESTS: frozenset[str] = frozenset(
    {
        "015f7e6bc5aeaf48", "02f3c8e9896c0ab9", "044f74822bd63a2c", "0681c0b3731722d0", "06fd2a9bd4249210",
        "07a06feaaf9beac6", "084a4bd8972f8d49", "08bb42a30ca57f34", "0dc8558d4e8f58fd", "0e6ba33f8bc8f415",
        "0e79172671f59f0f", "0ef514fa6b0ba4de", "0fdcd69a6565f341", "0feaa8fbb81101dd", "101c79255f7a7730",
        "104f75b18b5de3f1", "13032acd8761af05", "1317dfa6a0c51245", "1508b697895abf03", "154c2b744d9fb388",
        "1601f6b3d4d0fe78", "16c282b329b86400", "16e2a0597646c75d", "16ec9e273c66753b", "1742b64ed29107ac",
        "18b659028f0c7588", "1955c6da70a9cfa0", "19af2f572ff37300", "19f69dca423bacd9", "1a637788c9e469bd",
        "1aec32cd8d5193fe", "1af32fa0f14ba931", "1b5ba1dbdfe28bc0", "1bad4ee2e7a8e61e", "1e103fdeb16ccc3e",
        "2039f6325e44ca40", "205132cdb8279369", "247c3845d1f584be", "2511cdf9880733c0", "2565d561d22927f6",
        "28cc2c28738006c5", "2acf268f0425ad58", "2ae8e3db9922e385", "2ca8fb055500fe44", "2d51e570555cef3f",
        "2dcb808d957eaa15", "2df7b03d1a69954f", "2e4ad53f470aeb6f", "2f5e41ca731f8851", "304b4a90a76a1cbe",
        "3062f522f304ff90", "30699063f13e1fba", "307c6ce2052d8d79", "31af830247b2568e", "348a629f5ceed032",
        "358ff2a98f59b980", "386382fd0aa633fa", "39074ae4d2325ee3", "39a92f98f7794ee2", "3a561ffb16a70527",
        "3a675c66be82508c", "3a7bd3e2360a3d29", "3aff28c73f5050dd", "3b296edfdae61151", "3bb30e3f028cf193",
        "3c26104b9c3a9305", "3e1f0d98cb5b230e", "3f22486822d9e28a", "3f367a1caccdb20e", "3fec2131852b5db5",
        "405e5361592f0d56", "41559c72e9fb9d40", "416a90af68678596", "42ee150e0f7c4751", "430667fb1c4cef1e",
        "4430d8fed63ee3c4", "45a2ac7d4b909e9b", "46c759e871935f8c", "47acc6cf81adff29", "48a91a363d070f88",
        "4b67eb9e246ec1b0", "4ede9faa46c962d7", "4f69dd532490f3cf", "4fddfe8b9f2fffef", "503d8bfc1e907f84",
        "509398e69a50e4cd", "511b5f4a69ba0613", "5313dcd2d76e53df", "53314eb3760b340b", "537263c2bc830a08",
        "53fad9f4c32639cf", "54b3cc365051676b", "54d5dbf0124e59af", "5534b9f109434ce4", "5603e95c95e68470",
        "567dc439685eae82", "5697abca7a318e68", "569a0ccecd72cd52", "56e44b0439f78d81", "587d392b6c7680ec",
        "59f006d63bd009b7", "5a4678b1389b99a2", "5a7b2e919d9eb13c", "5b24c087276422bf", "5dd95c98aff2e783",
        "5e4fd7c24ecb9eba", "647308d903007051", "655eab9f02534cf0", "661bb40a733e6184", "675f4ef1b0d69fd0",
        "67ccf48a3c8cb30c", "6815f3c300383519", "69af0d80eb20c1f9", "69fd3e2252d8c4d2", "6a930006ef296ea0",
        "6bcbb14c1cad578f", "6cfd2c202f3ce6bd", "6e89adf712dc8f28", "6f80c60267f6bc73", "7124002c99d1c63e",
        "7142041d842dd25c", "721256f637634a36", "72401749a062683a", "732bf9b4ad2950ba", "7397437495f2fe22",
        "7427c84e7973d071", "7572939018fd775e", "7645981211a490eb", "777a535c1de3855f", "77c01eac595d524c",
        "79082e07de34a101", "7937c4dc4579bfc7", "7a9cb0b2fad22578", "7bb73cf8ed6137de", "7bf3a36acfa04eae",
        "7d3fe07842864368", "7d5c922f1f23aabc", "7dfea7361415a819", "7e20fdb3e3dca6a5", "7ee9757fe856d858",
        "7f35cc1890301903", "7f6feaddf6aa03ec", "801321025a0d9165", "821fc06fb84cbb14", "827c0fb1ae67b3ae",
        "82eee9303b51096a", "834d79caa6e0efb1", "8371a4903517847d", "8448ebc85d4b37e5", "85ddfb95cc744e06",
        "86505085bb4773d5", "86acf6296186d44c", "873b56afdd5edab9", "87b5af729f4cee3e", "87c84cdcddd66591",
        "8c311cc940959407", "8cfde6efdfc4ed5a", "8db0f4293d7f8590", "8e3b6fd7bc43cbab", "8e69adb916b68f96",
        "8e89d17476eacc6e", "8eacdfd26662076c", "903007c04c9719c2", "9031a5b72ec0a18c", "9045e5ee6075b629",
        "9264d1a7ad3521f7", "93fa0221c43e31c5", "93ffadbd82a7e144", "943e9cd57516d500", "9556b531bfe6d557",
        "955b98b535a53fef", "968e2d5b08687bf4", "969e8df6f8437bae", "98ad55be7b73192a", "9ab5a0695b4414b3",
        "9b200e506f1852fe", "9bd2fd2f30a9da68", "9be28bbe03fa7c43", "9c29a5917cf73dce", "9d8b8d6c615c6ee7",
        "9e506c686ab8eda2", "9f3a4329d8325465", "a0302f70b937755f", "a0598c92a763e8c9", "a0f7485d49293dfb",
        "a1335fc95481ca6a", "a1cd94c4a9c0fa36", "a21e28dee6025730", "a33a3059ad6d9ce3", "a3cb3c8587a702ff",
        "a5e7c002443743c5", "a6434726c118c8b2", "a7444ee050c62abd", "a78be3f471db8353", "a8075c9a62c43937",
        "a8945f9de7012c3f", "a96bf4d0c5ea465e", "ac627ed0af30b5a7", "ac85892205b92e8c", "add7cb9d9e6274e6",
        "ae31d95ed521c35f", "af20a4cdb149e87b", "b21492b15d85500e", "b46bcc668e54b0de", "b82e2178ecf4d0c5",
        "b93d038977e93959", "bb45e55a4f040e05", "bbd4b13180c53057", "bc236a8abd26671b", "bc3777ca4c17a6e3",
        "bcfdaddc64c3d9c9", "bd40e3bf63110e3a", "bf61abd9c8c94e02", "bf9ea44c6de01fdf", "bfc38d9e07ef81c2",
        "c06f5d9112e70a05", "c0711608bf823626", "c195d2d875623436", "c4752a95766d6df5", "c55637c63bba5928",
        "c608ee44356ec013", "c645c24ac08dd432", "c7bd0733df865ceb", "c82f6445e62496c3", "c9015464958c5c55",
        "ca6e0dc328e4570d", "cc98764ac015c840", "ce496a3d0dde19b9", "ce8b09469c47d9fe", "cf092e069b6e360e",
        "cfc90b2af0014a4d", "d0554f00d096f825", "d098ab5e44b9aabb", "d1e14a1b8d1120d2", "d601e2e7df4577a7",
        "d735fb5e9f2f61a3", "d796ef086cce6b78", "d7b2378b30b4dc40", "d7c067eb81618427", "d86fb264f0f30874",
        "d944bfb18e1258bf", "db669af634b75c7f", "dbba921ce2051021", "dd07d1a883099420", "dd3294b858203c2b",
        "dd4cb9cf52f0a9f1", "de01366e0265a599", "de7556eb9e35ccbe", "deae68ddceb684b8", "df876e7801ac592f",
        "dfac091ba9d0b40b", "e004e17f11fcfab1", "e06b5fc81708477b", "e0d7680971095e5a", "e0f167bc84b881bc",
        "e2bd9c6236a519d4", "e30d07a46c348d73", "e3513d48cef3a53d", "e351a4e02aff31e3", "e5d81ee5df37e7c0",
        "e5e82a724df2d915", "e6b817324ca44ccb", "e6f3444cd8e0e524", "e7a7ec96c5c5a492", "e83638d636c7e1f4",
        "e86946df1aed0f8f", "e97469619c0d7aa7", "ec27f74729c97a09", "ec5c41c127a484eb", "eefa3980fdf345e9",
        "f62199d58f0e7156", "f62ca90d76a18f31", "f685a6457d0830b8", "f6b007952885d3fc", "f750636ca108c9cd",
        "f955f62dab66faee", "fa2158dc213ff71b", "fa44132b238e6795", "fa6598317163f260", "fb1d2d7b10245525",
        "fb6649676eb8bd40", "fbd6ba8267c9de70", "fc17bc013d0a7f76", "fd244bcd2f2cbaf1", "fd6b97da297f1c04",
        "fdb6ca387308b768", "fe3d2b354f38c924", "ff36ee5a67a7cd80", "ffb5cfd2780df55d",
    }
)  # fmt: skip


@dataclass
class Brand:
    brand_id: str
    brand_name: str
    category: str
    price_tier: int
    anchor: bool
    target_area_min_m2: float
    target_area_max_m2: float
    preferred_floors: list[str] = field(default_factory=list)
    adjacency_likes: list[str] = field(default_factory=list)
    adjacency_conflicts: list[str] = field(default_factory=list)
    min_frontage_m: float = 3.0
    attractiveness: float = 1.0


def normalize_name(name: str) -> str:
    """Lowercase, strip accents and keep only ASCII alphanumerics and Hangul syllables."""
    text = unicodedata.normalize("NFKD", name)
    text = unicodedata.normalize("NFKC", "".join(ch for ch in text if not unicodedata.combining(ch)))
    return re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", text.lower())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_blocked_name(name: str) -> bool:
    """True when the name, any token, or any adjacent token pair matches the blocklist."""
    tokens = [normalize_name(t) for t in re.split(r"[\s&\-_.]+", name)]
    tokens = [t for t in tokens if t]
    candidates = {normalize_name(name), *tokens}
    candidates.update(a + b for a, b in zip(tokens, tokens[1:], strict=False))
    return any(_digest(c) in _BLOCKED_DIGESTS for c in candidates if c)


def default_brand_counts() -> dict[str, int]:
    """About 1.5x the expected number of units per category across all floors, plus spares."""
    expected = dict.fromkeys(CATEGORIES, 0.0)
    for program in FLOOR_PROGRAMS:
        for code, share in program.category_mix.items():
            lo, hi = CATEGORIES[code].unit_area_range_m2
            expected[code] += share * NOMINAL_TENANT_AREA_M2 / ((lo + hi) / 2)
    return {code: math.ceil(SPARE_FACTOR * n) + MIN_SPARES for code, n in expected.items()}


def _preferred_floors(category: str) -> list[str]:
    weighted = [(p.category_mix[category], p.code) for p in FLOOR_PROGRAMS if category in p.category_mix]
    return [code for _, code in sorted(weighted, key=lambda item: -item[0])]


def _price_tier(rng: np.random.Generator, mean_ticket_krw: int) -> int:
    if mean_ticket_krw >= 300_000:
        probs = (0.05, 0.25, 0.70)
    elif mean_ticket_krw >= 100_000:
        probs = (0.20, 0.50, 0.30)
    else:
        probs = (0.50, 0.40, 0.10)
    return int(rng.choice([1, 2, 3], p=probs))


def _make_name(rng: np.random.Generator, category: str) -> str:
    template = str(rng.choice(_TEMPLATES[category]))

    def en_word() -> str:
        return str(rng.choice(_EN_STEMS)) + str(rng.choice(_EN_ENDINGS))

    name = template.replace("{E2}", en_word()).replace("{E}", en_word())
    return name.replace("{K}", str(rng.choice(_KO_STEMS)))


def generate_brands(seed: int, counts: dict[str, int] | None = None) -> list[Brand]:
    """Generate a deterministic list of fictional brands (``BR0001``...) grouped by category."""
    counts = counts or default_brand_counts()
    rng = np.random.default_rng([seed, 7_001])
    used: set[str] = set()
    brands: list[Brand] = []
    for code, category in CATEGORIES.items():
        n = counts.get(code, 0)
        drafts: list[tuple[str, float]] = []
        attempts = 0
        while len(drafts) < n:
            attempts += 1
            if attempts > 200 * (n + 10):
                raise RuntimeError(f"cannot generate {n} unique names for {code}")
            name = _make_name(rng, code)
            key = normalize_name(name)
            if key in used or is_blocked_name(name):
                continue
            used.add(key)
            attractiveness = float(np.clip(rng.lognormal(0.0, 0.22), 0.6, 1.6))
            drafts.append((name, round(attractiveness, 3)))
        n_anchor = max(1, round(0.12 * n)) if n else 0
        anchor_names = {name for name, _ in sorted(drafts, key=lambda d: -d[1])[:n_anchor]}
        lo, hi = category.unit_area_range_m2
        mid = (lo + hi) / 2
        floors = _preferred_floors(code)
        likes = list(ADJACENCY_LIKES.get(code, ()))
        for name, attractiveness in drafts:
            area_min = round(float(rng.uniform(lo, mid)) / 5) * 5
            area_max = round(float(rng.uniform(mid, hi)) / 5) * 5
            n_likes = int(rng.integers(1, len(likes) + 1)) if likes else 0
            anchor = name in anchor_names
            brands.append(
                Brand(
                    brand_id=f"BR{len(brands) + 1:04d}",
                    brand_name=name,
                    category=code,
                    price_tier=_price_tier(rng, category.mean_ticket_krw),
                    anchor=anchor,
                    target_area_min_m2=float(area_min),
                    target_area_max_m2=float(max(area_max, area_min)),
                    preferred_floors=list(floors),
                    adjacency_likes=likes[:n_likes],
                    adjacency_conflicts=list(ADJACENCY_CONFLICTS.get(code, ())),
                    min_frontage_m=round(float(np.clip(math.sqrt(area_min) * 0.6, 3.0, 15.0)), 1),
                    attractiveness=min(1.6, round(attractiveness * (1.1 if anchor else 1.0), 3)),
                )
            )
    return brands


def brands_to_rows(brands: list[Brand]) -> list[dict[str, Any]]:
    """Flatten brands for CSV export (list fields are ``;``-joined)."""
    return [
        {
            "brand_id": b.brand_id,
            "brand_name": b.brand_name,
            "category": b.category,
            "category_name": CATEGORIES[b.category].name_ko,
            "price_tier": b.price_tier,
            "anchor": b.anchor,
            "target_area_min_m2": b.target_area_min_m2,
            "target_area_max_m2": b.target_area_max_m2,
            "preferred_floors": ";".join(b.preferred_floors),
            "adjacency_likes": ";".join(b.adjacency_likes),
            "adjacency_conflicts": ";".join(b.adjacency_conflicts),
            "min_frontage_m": b.min_frontage_m,
            "attractiveness": b.attractiveness,
        }
        for b in brands
    ]
