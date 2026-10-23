"""Drawing profiles: configurable drawing conventions for the CAD parser (docs/specs/drawing-profile.md).

A profile tells the parser how a source of drawings writes unit codes, zone labels, vertical-link and opening
IDs and vacancies, which layer names carry which meaning, and which units the coordinates use. Codes found
with a profile are rewritten to the canonical Atlas form (``6F-C05``) through ``format`` templates and maps.

``DEFAULT_PROFILE`` reproduces the synthetic conventions (docs/specs/cad-conventions.md) exactly. Uploaded
profiles live in the workspace at ``business/drawing_profiles/{profile_id}.yaml``. The parser reads the
active profile through a context variable; ``use_profile`` activates one for a block of code.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cad2anything.cadcore.reader import UNIT_NAMES_TO_M

# --- default (synthetic) conventions; labels.py re-exports these names -----------------------------------

UNIT_CODE_RE = re.compile(r"^(B\d|\d+F)-[NSEWCP]\d{2}[A-Z]?M?$")
UNIT_CODE_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9])(?:B\d+|\d+F)-[NSEWCP]\d{2}[A-Z]?M?(?![A-Za-z0-9])")
BARE_UNIT_CODE_RE = re.compile(r"^[NSEWCP]\d{2}[A-Z]?M?$")
ZONE_RE = re.compile(r"^\s*(?P<code>(?:(?:B\d+|\d+F)-)?Z\d+)(?![A-Za-z0-9])\s*(?P<name>.*)$", re.DOTALL)
LINK_RE = re.compile(r"(?<![A-Za-z0-9])(?P<id>(?:ESC|EV|ST)-[A-Z0-9]{1,3})(?![A-Za-z0-9])")
OPENING_RE = re.compile(r"(?<![A-Za-z0-9])(?P<id>(?:ENT|DOOR|GATE|EXIT)-[A-Z0-9]{1,4})(?![A-Za-z0-9])")
_VACANCY_WORDS = r"공실|VACANT|임대\s*중|임대\s*예정"
VACANCY_RE = re.compile(rf"^[\(\[]?\s*({_VACANCY_WORDS})\s*[\)\]]?$", re.IGNORECASE)
LINK_PREFIX_TYPES: dict[str, str] = {"ESC": "escalator", "EV": "elevator", "ST": "stair"}
LINK_WORDS: dict[str, str] = {
    "에스컬레이터": "escalator",
    "ESCALATOR": "escalator",
    "엘리베이터": "elevator",
    "승강기": "elevator",
    "ELEVATOR": "elevator",
    "LIFT": "elevator",
    "계단": "stair",
    "STAIR": "stair",
    "STAIRS": "stair",
}
LINK_TYPES: tuple[str, ...] = ("escalator", "elevator", "stair")

DEFAULT_PROFILE_ID = "default"
PROFILE_DIR = "business/drawing_profiles"
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PROFILE_KEYS: tuple[str, ...] = (
    "profile_id",
    "title",
    "description",
    "units",
    "layers",
    "floor_aliases",
    "unit_code",
    "zone",
    "vertical_link",
    "opening",
    "vacancy",
)
MAX_PATTERN_CHARS = 500
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BASEMENT = re.compile(r"^(?:지하|B)(\d{1,2})(?:층|F)?$")
_ABOVE = re.compile(r"^(?:지상)?(\d{1,3})(?:층|F)$")
_DIGITS = re.compile(r"^\d{1,3}$")


# --- helpers ----------------------------------------------------------------------------------------------


def _compact(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).upper()


def map_key(text: Any) -> str:
    """Lookup key for code maps: whitespace removed, upper case."""
    return _compact(text)


def normalize_floor(
    text: Any, aliases: Mapping[str, str] | None = None, *, allow_bare: bool = False
) -> str | None:
    """Canonical floor code (``4F``, ``B1``) from ``4층``, ``4F``, ``4f``, ``지하1층``, ``B1F`` or ``B1``.

    ``aliases`` (keys compared without spaces, upper case) win over the built-in rules. Bare numbers (``4``) are
    floors only with ``allow_bare``. Returns None when ``text`` is not a floor.
    """
    compact = _compact(text)
    if not compact:
        return None
    if aliases and compact in aliases:
        return aliases[compact]
    if match := _BASEMENT.match(compact):
        number = int(match.group(1))
        return f"B{number}" if number else None
    if (match := _ABOVE.match(compact)) or (allow_bare and (match := _DIGITS.match(compact))):
        number = int(match.group(1) if match.groups() else match.group(0))
        return f"{number}F" if number else None
    return None


def _render(template: str, fields: Mapping[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: fields.get(m.group(1), ""), template)


def _pattern_text(pattern: re.Pattern[str]) -> str:
    """Pattern source with its flags inlined, so it can be stored as text and compiled again."""
    flags = "".join(
        letter
        for flag, letter in ((re.IGNORECASE, "i"), (re.MULTILINE, "m"), (re.DOTALL, "s"))
        if pattern.flags & flag
    )
    return f"(?{flags}){pattern.pattern}" if flags else pattern.pattern


# --- profile ----------------------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class DrawingProfile:
    """Drawing conventions of one drawing source. Build with ``load_profile``; ``spec`` is the stored form."""

    profile_id: str
    title: str
    description: str = ""
    units: str | None = None
    layers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    floor_aliases: Mapping[str, str] = field(default_factory=dict)
    unit_pattern: re.Pattern[str] = UNIT_CODE_IN_TEXT_RE
    unit_bare: re.Pattern[str] | None = BARE_UNIT_CODE_RE
    unit_format: str | None = None
    unit_canonical: re.Pattern[str] | None = UNIT_CODE_RE
    unit_map: Mapping[str, str] = field(default_factory=dict)
    zone_pattern: re.Pattern[str] = ZONE_RE
    zone_format: str | None = None
    link_pattern: re.Pattern[str] = LINK_RE
    link_format: str | None = None
    link_map: Mapping[str, str] = field(default_factory=dict)
    link_types: Mapping[str, str] = field(default_factory=lambda: dict(LINK_PREFIX_TYPES))
    link_words: Mapping[str, str] = field(default_factory=lambda: dict(LINK_WORDS))
    opening_pattern: re.Pattern[str] = OPENING_RE
    opening_format: str | None = None
    opening_map: Mapping[str, str] = field(default_factory=dict)
    vacancy_pattern: re.Pattern[str] = VACANCY_RE
    spec: Mapping[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "title": self.title,
            "description": self.description,
            "units": self.units,
        }

    def floor_code(self, text: Any) -> str:
        """Canonical floor of a captured floor text (aliases, then built-in rules, else the text itself)."""
        return normalize_floor(text, self.floor_aliases, allow_bare=True) or _compact(text)

    def _fields(self, match: re.Match[str], floor: str | None) -> dict[str, str]:
        fields = {name: value or "" for name, value in match.groupdict().items()}
        fields["floor"] = self.floor_code(fields["floor"]) if fields.get("floor") else (floor or "")
        return fields

    def unit_code(self, match: re.Match[str], floor: str | None = None) -> str:
        """Canonical unit code for a ``unit_pattern`` match (``floor``: the drawing's floor, if known)."""
        mapped = self.unit_map.get(map_key(match.group(0)))
        if mapped is not None:
            return mapped
        if self.unit_format is None:
            return match.group(0)
        code = _render(self.unit_format, self._fields(match, floor))
        return self.unit_map.get(map_key(code), code)

    def bare_unit_code(self, word: str, floor: str) -> str | None:
        """Canonical unit code for a label whose first word is a code without floor, else None."""
        if self.unit_bare is None:
            return None
        match = self.unit_bare.fullmatch(word)
        if match is None:
            return None
        code = (
            f"{floor}-{word}"
            if self.unit_format is None
            else _render(self.unit_format, self._fields(match, floor))
        )
        return self.unit_map.get(map_key(code), code)

    def is_nonstandard(self, code: str) -> bool:
        """True when ``code`` does not have the canonical form (codes built by ``format`` are canonical)."""
        return self.unit_canonical is not None and not self.unit_canonical.match(code)

    def zone_label(self, text: str, floor: str) -> tuple[str, str] | None:
        """``(zone_code, zone_name)`` of a zone label, else None."""
        match = self.zone_pattern.match(text.strip())
        if not match:
            return None
        groups = match.groupdict()
        if self.zone_format is None:
            code = groups.get("code") or match.group(0).strip()
            if "-" not in code:
                code = f"{floor}-{code}"
        else:
            code = _render(self.zone_format, self._fields(match, floor))
        return code, " ".join((groups.get("name") or "").split())

    @staticmethod
    def _coded_id(match: re.Match[str], template: str | None, mapping: Mapping[str, str]) -> str:
        groups = match.groupdict()
        raw = groups.get("id") or match.group(0)
        mapped = mapping.get(map_key(raw))
        if mapped is not None:
            return mapped
        if template is None:
            return raw
        code = _render(template, {name: value or "" for name, value in groups.items()})
        return mapping.get(map_key(code), code)

    def link_id(self, match: re.Match[str]) -> str:
        """Canonical vertical-link ID (same on every floor, e.g. ``ESC-A``) for a ``link_pattern`` match."""
        return self._coded_id(match, self.link_format, self.link_map)

    def link_type(self, link_id: str) -> str | None:
        return self.link_types.get(map_key(link_id.split("-")[0]))

    def opening_id(self, match: re.Match[str]) -> str:
        """Canonical opening ID (e.g. ``ENT-1``) for an ``opening_pattern`` match."""
        return self._coded_id(match, self.opening_format, self.opening_map)


DEFAULT_SPEC: dict[str, Any] = {
    "profile_id": DEFAULT_PROFILE_ID,
    "title": "기본 규약(합성 도면)",
    "description": "Atlas 합성 도면 규약이다. 매장 코드는 3F-C05, 존은 3F-Z1, 수직동선은 ESC-A, 출입구는 ENT-1 형식이다.",
    "unit_code": {
        "pattern": UNIT_CODE_IN_TEXT_RE.pattern,
        "bare": BARE_UNIT_CODE_RE.pattern,
        "canonical": UNIT_CODE_RE.pattern,
    },
    "zone": {"pattern": _pattern_text(ZONE_RE)},
    "vertical_link": {"pattern": LINK_RE.pattern},
    "opening": {"pattern": OPENING_RE.pattern},
    "vacancy": {"pattern": _pattern_text(VACANCY_RE)},
}
DEFAULT_PROFILE = DrawingProfile(
    profile_id=DEFAULT_PROFILE_ID,
    title=DEFAULT_SPEC["title"],
    description=DEFAULT_SPEC["description"],
    spec=DEFAULT_SPEC,
)

# --- activation -------------------------------------------------------------------------------------------

_ACTIVE: ContextVar[DrawingProfile | None] = ContextVar("cad2anything_drawing_profile", default=None)


def active_profile() -> DrawingProfile:
    """The profile activated with ``use_profile`` in this context, else ``DEFAULT_PROFILE``."""
    return _ACTIVE.get() or DEFAULT_PROFILE


@contextmanager
def use_profile(profile: DrawingProfile | None) -> Iterator[DrawingProfile]:
    """Activate ``profile`` (None: the default) for the label and classification functions."""
    token = _ACTIVE.set(profile)
    try:
        yield profile or DEFAULT_PROFILE
    finally:
        _ACTIVE.reset(token)


# --- validation -------------------------------------------------------------------------------------------


class _Checker:
    """Collects Korean error and warning messages while normalizing one profile document."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def text(self, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    def regex(self, value: Any, where: str) -> re.Pattern[str] | None:
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"{where}는 비어 있지 않은 정규식 문자열이어야 합니다.")
            return None
        if len(value) > MAX_PATTERN_CHARS:
            self.errors.append(f"{where} 정규식이 너무 깁니다(최대 {MAX_PATTERN_CHARS}자).")
            return None
        try:
            pattern = re.compile(value)
        except re.error as exc:
            self.errors.append(f"{where} 정규식 오류: {exc}")
            return None
        if pattern.search("") is not None:
            self.errors.append(f"{where} 정규식이 빈 문자열과도 일치합니다. 더 구체적으로 써야 합니다.")
            return None
        return pattern

    def template(self, value: Any, where: str, groups: set[str]) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            self.errors.append(f"{where}는 비어 있지 않은 문자열이어야 합니다.")
            return None
        leftover = _PLACEHOLDER.sub("", value)
        if "{" in leftover or "}" in leftover:
            self.errors.append(f"{where}에는 {{이름}} 형식의 자리표시자만 쓸 수 있습니다: {value}")
            return None
        missing = sorted(set(_PLACEHOLDER.findall(value)) - groups - {"floor"})
        if missing:
            names = ", ".join(f"{{{name}}}" for name in missing)
            self.errors.append(
                f"{where}의 {names}에 해당하는 이름 붙은 그룹(?P<이름>...)이 정규식에 없습니다."
            )
            return None
        return value

    def mapping(self, value: Any, where: str, upper_values: bool = False) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            self.errors.append(f"{where}는 키-값 매핑이어야 합니다.")
            return {}
        result: dict[str, str] = {}
        for key, item in value.items():
            target = self.text(item)
            if not self.text(key) or not target:
                self.errors.append(f"{where}에 빈 키나 값이 있습니다: {key!r}: {item!r}")
                continue
            result[map_key(key)] = target.upper() if upper_values else target
        return result

    def section(self, value: Any, name: str, keys: tuple[str, ...]) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            self.errors.append(f"{name}는 키-값 매핑이어야 합니다.")
            return None
        unknown = [str(key) for key in value if key not in keys]
        if unknown:
            self.warnings.append(f"{name}의 알 수 없는 항목은 무시합니다: {', '.join(unknown)}")
        if "pattern" not in value and name != "vacancy":
            self.errors.append(f"{name}.pattern이 필요합니다.")
            return None
        return dict(value)


def _layers(checker: _Checker, value: Any) -> dict[str, tuple[str, ...]]:
    from cad2anything.cadcore.classify import SEMANTICS

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        checker.errors.append("layers는 의미 → 레이어 이름(목록) 매핑이어야 합니다.")
        return {}
    layers: dict[str, tuple[str, ...]] = {}
    owner: dict[str, str] = {}
    for semantic, names in value.items():
        semantic = str(semantic).strip()
        if semantic not in SEMANTICS:
            checker.errors.append(
                f"layers의 의미 '{semantic}'를 알 수 없습니다. 쓸 수 있는 값: {', '.join(SEMANTICS)}"
            )
            continue
        items = [names] if isinstance(names, str) else names
        if not isinstance(items, list | tuple) or not items:
            checker.errors.append(f"layers.{semantic}는 레이어 이름이나 이름 목록이어야 합니다.")
            continue
        cleaned: list[str] = []
        for name in items:
            text = checker.text(name)
            if not text:
                checker.errors.append(f"layers.{semantic}에 빈 레이어 이름이 있습니다.")
                continue
            if owner.get(text, semantic) != semantic:
                checker.errors.append(
                    f"레이어 '{text}'가 여러 의미({owner[text]}, {semantic})에 연결되어 있습니다."
                )
                continue
            owner[text] = semantic
            if text not in cleaned:
                cleaned.append(text)
        if cleaned:
            layers[semantic] = tuple(cleaned)
    return layers


def _floor_aliases(checker: _Checker, data: Mapping[str, Any]) -> dict[str, str]:
    aliases = checker.mapping(data.get("floor_aliases"), "floor_aliases")
    unit = data.get("unit_code")
    if isinstance(unit, Mapping):
        aliases.update(checker.mapping(unit.get("floor_aliases"), "unit_code.floor_aliases"))
    return {key: normalize_floor(value) or _compact(value) for key, value in aliases.items()}


def _stored(section: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: section[key] for key in keys if section.get(key) not in (None, "", {}, [])}


def _unit_section(checker: _Checker, value: Any, fields: dict[str, Any], spec: dict[str, Any]) -> None:
    keys = ("pattern", "bare", "format", "canonical", "map", "floor_aliases")
    section = checker.section(value, "unit_code", keys)
    if section is None:
        return
    pattern = checker.regex(section.get("pattern"), "unit_code.pattern")
    bare = checker.regex(section["bare"], "unit_code.bare") if section.get("bare") is not None else None
    canonical = None
    if section.get("canonical") is not None:
        canonical = checker.regex(section["canonical"], "unit_code.canonical")
    if pattern is None:
        return
    template = checker.template(section.get("format"), "unit_code.format", set(pattern.groupindex))
    if template is not None and bare is not None:
        checker.template(template, "unit_code.format(bare 기준)", set(bare.groupindex))
    if section.get("format") is None and "floor" in pattern.groupindex:
        checker.warnings.append("unit_code.format이 없어 패턴이 찾은 문자를 그대로 매장 코드로 씁니다.")
    fields.update(
        unit_pattern=pattern,
        unit_bare=bare,
        unit_format=template,
        # Codes built by ``format`` are canonical by definition; verbatim codes are checked against the default.
        unit_canonical=canonical if canonical is not None else (None if template else UNIT_CODE_RE),
        unit_map=checker.mapping(section.get("map"), "unit_code.map"),
    )
    spec["unit_code"] = _stored(section, keys[:-1])


_CODED_KEYS: dict[str, tuple[str, ...]] = {
    "zone": ("pattern", "format"),
    "vertical_link": ("pattern", "format", "map", "types", "words"),
    "opening": ("pattern", "format", "map"),
}


def _coded_section(
    checker: _Checker, value: Any, name: str, prefix: str, fields: dict[str, Any], spec: dict[str, Any]
) -> None:
    """``zone``, ``vertical_link`` and ``opening`` sections (pattern, optional format and map)."""
    keys = _CODED_KEYS[name]
    section = checker.section(value, name, keys)
    if section is None:
        return
    pattern = checker.regex(section.get("pattern"), f"{name}.pattern")
    if pattern is None:
        return
    template = checker.template(section.get("format"), f"{name}.format", set(pattern.groupindex))
    if name == "zone" and section.get("format") is None and "code" not in pattern.groupindex:
        checker.errors.append("zone.pattern에는 (?P<code>...) 그룹이 있거나 zone.format이 있어야 합니다.")
        return
    fields[f"{prefix}_pattern"] = pattern
    fields[f"{prefix}_format"] = template
    if "map" in keys:
        fields[f"{prefix}_map"] = checker.mapping(section.get("map"), f"{name}.map", upper_values=True)
    if name == "vertical_link":
        types = checker.mapping(section.get("types"), "vertical_link.types")
        words = checker.mapping(section.get("words"), "vertical_link.words")
        bad = sorted({kind for kind in [*types.values(), *words.values()] if kind not in LINK_TYPES})
        if bad:
            checker.errors.append(
                f"vertical_link의 수직동선 종류는 {', '.join(LINK_TYPES)} 중 하나여야 합니다: {', '.join(bad)}"
            )
        fields["link_types"] = {**LINK_PREFIX_TYPES, **types}
        fields["link_words"] = {**LINK_WORDS, **words}
    spec[name] = _stored(section, keys)


def _vacancy_section(checker: _Checker, value: Any, fields: dict[str, Any], spec: dict[str, Any]) -> None:
    section = checker.section(value, "vacancy", ("pattern", "words"))
    if section is None:
        return
    if section.get("pattern") is not None:
        pattern = checker.regex(section["pattern"], "vacancy.pattern")
        if section.get("words"):
            checker.warnings.append("vacancy.pattern이 있으면 vacancy.words는 쓰지 않습니다.")
        if pattern is not None:
            fields["vacancy_pattern"] = pattern
            spec["vacancy"] = {"pattern": section["pattern"]}
        return
    words = section.get("words")
    if isinstance(words, str):
        words = [words]
    if not isinstance(words, list | tuple) or not all(checker.text(w) for w in words):
        checker.errors.append("vacancy.words는 공실 표기 문자열 목록이어야 합니다.")
        return
    extra = [r"\s*".join(re.escape(part) for part in checker.text(w).split()) for w in words]
    fields["vacancy_pattern"] = re.compile(
        rf"^[\(\[]?\s*({_VACANCY_WORDS}|{'|'.join(extra)})\s*[\)\]]?$", re.IGNORECASE
    )
    spec["vacancy"] = {"words": [checker.text(w) for w in words]}


def validate_profile(data: Any) -> tuple[DrawingProfile | None, list[str], list[str]]:
    """``(profile or None, errors, warnings)`` for a profile document (Korean messages)."""
    checker = _Checker()
    if not isinstance(data, Mapping):
        return None, ["프로필은 키-값 객체(YAML 매핑)여야 합니다."], []
    unknown = [str(key) for key in data if key not in PROFILE_KEYS]
    if unknown:
        checker.warnings.append(f"알 수 없는 항목은 무시합니다: {', '.join(unknown)}")
    profile_id = checker.text(data.get("profile_id"))
    if not profile_id:
        checker.errors.append("필수 항목 profile_id가 없습니다.")
    elif not PROFILE_ID_RE.match(profile_id):
        checker.errors.append(
            "profile_id는 영문 소문자·숫자·-·_로 1~40자이고 영문 소문자나 숫자로 시작해야 합니다."
        )
    title = checker.text(data.get("title"))
    if not title:
        checker.errors.append("필수 항목 title이 없습니다.")
    description = checker.text(data.get("description"))
    units = checker.text(data.get("units")).lower() or None
    if units is not None and units not in UNIT_NAMES_TO_M:
        checker.errors.append(f"units는 {', '.join(sorted(UNIT_NAMES_TO_M))} 중 하나여야 합니다: {units}")
    fields: dict[str, Any] = {
        "layers": _layers(checker, data.get("layers")),
        "floor_aliases": _floor_aliases(checker, data),
    }
    spec: dict[str, Any] = {"profile_id": profile_id, "title": title}
    if description:
        spec["description"] = description
    if units:
        spec["units"] = units
    if fields["layers"]:
        spec["layers"] = {semantic: list(names) for semantic, names in fields["layers"].items()}
    if fields["floor_aliases"]:
        spec["floor_aliases"] = dict(fields["floor_aliases"])
    _unit_section(checker, data.get("unit_code"), fields, spec)
    _coded_section(checker, data.get("zone"), "zone", "zone", fields, spec)
    _coded_section(checker, data.get("vertical_link"), "vertical_link", "link", fields, spec)
    _coded_section(checker, data.get("opening"), "opening", "opening", fields, spec)
    _vacancy_section(checker, data.get("vacancy"), fields, spec)
    if checker.errors:
        return None, checker.errors, checker.warnings
    profile = DrawingProfile(
        profile_id=profile_id, title=title, description=description, units=units, spec=spec, **fields
    )
    return profile, checker.errors, checker.warnings


def load_profile(source: Mapping[str, Any] | str | Path) -> DrawingProfile:
    """Profile from a document or a YAML/JSON file path; raises ``ValueError`` (Korean) when invalid."""
    data: Any = source
    if isinstance(source, str | Path):
        data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    profile, errors, _ = validate_profile(data)
    if profile is None:
        raise ValueError("도면 규약 프로필이 올바르지 않습니다: " + " / ".join(errors))
    return profile


def profile_to_dict(profile: DrawingProfile) -> dict[str, Any]:
    """Stored form of a profile (``load_profile`` of the result behaves the same)."""
    return copy.deepcopy(dict(profile.spec))


def resolve_profile(
    profile: DrawingProfile | Mapping[str, Any] | None, manifest: Mapping[str, Any] | None = None
) -> DrawingProfile:
    """The given profile, else the manifest's inline ``drawing_profile``, else ``DEFAULT_PROFILE``."""
    if isinstance(profile, DrawingProfile):
        return profile
    if profile is not None:
        return load_profile(profile)
    inline = (manifest or {}).get("drawing_profile")
    return load_profile(inline) if inline else DEFAULT_PROFILE


def merge_layers(base: Mapping[str, Any] | None, override: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Semantic → layer names from two mappings; a layer name keeps the semantic of ``override`` on conflict."""
    owner: dict[str, str] = {}
    for mapping in (base or {}, override or {}):
        for semantic, names in dict(mapping).items():
            for name in [names] if isinstance(names, str) else list(names or []):
                owner[str(name)] = str(semantic)
    merged: dict[str, list[str]] = {}
    for name, semantic in owner.items():
        merged.setdefault(semantic, []).append(name)
    return merged
