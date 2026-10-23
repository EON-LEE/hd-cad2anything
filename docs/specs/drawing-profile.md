# 도면 규약 프로필

도면 규약 프로필은 도면마다 다른 표기 규약을 CAD 이해 파이프라인([cad-pipeline.md](cad-pipeline.md))에 알려 주는 문서다. 매장 코드, 존 라벨, 수직동선·출입구 ID, 공실 표기, 레이어 이름, 좌표 단위를 정한다. 파이프라인은 프로필로 찾은 코드를 Atlas 표준 코드(`6F-C05`, `6F-Z1`, `ESC-A`, `ENT-1`)로 바꾼다. 표준 코드 체계는 [cad-conventions.md](cad-conventions.md) 4절을 따른다.

## 1. 저장과 적용

| 항목 | 규약 |
|---|---|
| 형식 | YAML 또는 JSON 객체 하나 |
| 저장 위치 | 작업공간의 `business/drawing_profiles/{profile_id}.yaml` |
| 올리기 | 메타데이터 업로드 `kind=drawing_profile`([atlas-api.md](atlas-api.md) 1.2절). 같은 ID면 이전 파일을 `business/_backup/drawing_profiles/`에 보관하고 바꾼다. |
| 기본 프로필 | `default`. 합성 도면 규약이며 코드에 내장되어 있다. 저장하지 않고 바꿀 수 없다. |
| 도면 업로드 | 업로드할 때 `profile`로 프로필 ID를 고른다. 생성한 매니페스트의 `drawing_profile`에 프로필 전체를 넣어 둔다. |
| 파서 | `parse_revision(..., profile=)`에 프로필을 주면 그 프로필을 쓴다. 주지 않으면 매니페스트의 `drawing_profile`을 쓰고, 그것도 없으면 `default`를 쓴다. |

파서는 프로필을 컨텍스트 변수로 활성화한다(`cadcore.profile.use_profile`). 라벨 해석(`labels.py`)과 레이어 내용 특징(`classify.py`)이 모두 활성 프로필의 패턴을 쓴다. 따라서 프로필은 라벨 매칭뿐 아니라 레이어 의미 추정에도 영향을 준다.

## 2. 항목

| 키 | 필수 | 내용 |
|---|---|---|
| `profile_id` | 예 | 영문 소문자·숫자·`-`·`_`로 1~40자. 영문 소문자나 숫자로 시작한다. |
| `title` | 예 | 화면에 보일 이름 |
| `description` | | 설명 |
| `units` | | 도면 헤더(`$INSUNITS`)에 단위가 없을 때 쓰는 단위. `m`, `mm`, `cm` 등. 매니페스트 `units`가 있으면 매니페스트가 우선한다. |
| `layers` | | 의미 → 레이어 이름(문자열 또는 목록). 의미는 레이어 의미 16개 중 하나다. |
| `floor_aliases` | | 층 표기 → 표준 층 코드. 기본 규칙(3.2절)으로 바꿀 수 없는 층 이름만 적는다. 예: `M층: 2F` |
| `unit_code` | | 매장 코드 규칙(3절) |
| `zone` | | 존 라벨 규칙(4.1절) |
| `vertical_link` | | 수직동선 ID 규칙(4.2절) |
| `opening` | | 출입구 ID 규칙(4.3절) |
| `vacancy` | | 공실 표기 규칙(4.4절) |

- 적지 않은 항목은 `default`의 규칙을 쓴다. 다른 항목만 적으면 된다.
- 알 수 없는 키는 경고하고 무시한다.

## 3. 매장 코드(`unit_code`)

| 키 | 필수 | 내용 |
|---|---|---|
| `pattern` | 예 | 문자 안에서 매장 코드를 찾는 정규식. 문자 어디에서나 찾는다. 이름 붙은 그룹(`(?P<이름>...)`)을 `format`에 쓴다. |
| `bare` | | 층 없이 쓴 코드의 정규식. 라벨의 첫 단어 전체와 맞으면 도면의 층을 붙인다. |
| `format` | | 표준 코드를 만드는 형식. `{이름}` 자리표시자만 쓴다. `{floor}`는 표준 층 코드로 바뀐다. 패턴에 `floor` 그룹이 없으면 도면의 층을 쓴다. |
| `canonical` | | 표준 형식 검사 정규식. `format`이 있으면 생략할 수 있다. |
| `map` | | 도면 표기 → 표준 코드. 공백을 빼고 대문자로 비교한다. `format`보다 먼저 적용하고, `format` 결과에도 적용한다. |

### 3.1 처리 순서

1. 문자에서 `pattern`을 찾는다. 찾은 부분을 뺀 나머지는 브랜드명 또는 공실 표기다.
2. 찾지 못하면 첫 단어가 `bare`와 맞는지 본다.
3. 찾은 문자가 `map`에 있으면 그 값을 쓴다. 없으면 `format`으로 표준 코드를 만든다. `format`이 없으면 찾은 문자를 그대로 쓴다.
4. 표준 형식이 아닌 코드는 `unit_code_nonstandard`(info) 이슈로 남긴다. 기준은 `canonical`이다. `canonical`이 없으면 `format`으로 만든 코드는 표준으로 보고, `format`이 없으면 `default`의 표준 형식을 쓴다.

### 3.2 층 표기

`{floor}`로 쓰는 층 표기는 공백을 빼고 대문자로 바꾼 뒤 다음 순서로 표준 층 코드가 된다.

| 순서 | 규칙 | 예 |
|---|---|---|
| 1 | `floor_aliases` | `M층` → `2F` |
| 2 | 지하층 | `지하1층`, `B1`, `B1F`, `b1` → `B1` |
| 3 | 지상층 | `4층`, `4F`, `4f`, `지상4층` → `4F` |
| 4 | 숫자만 | `4` → `4F`(패턴 그룹 값일 때만) |

업로드한 파일 이름의 층(`R3_4F.dxf`, `4층.dxf`, `지하1층.dxf`)은 2~3번 규칙으로 읽는다. 파일 이름에서는 `floor_aliases`와 숫자만 있는 표기를 쓰지 않는다.

## 4. 그 밖의 코드

### 4.1 존(`zone`)

| 키 | 필수 | 내용 |
|---|---|---|
| `pattern` | 예 | 라벨 맨 앞부터 맞추는 정규식. `name` 그룹은 존 이름이다. |
| `format` | | 존 코드 형식. 예: `{floor}-Z{num}` |

`format`이 없으면 패턴에 `code` 그룹이 있어야 한다. `code`에 층이 없으면(`-`가 없으면) 도면의 층을 붙인다. 존 이름이 카테고리 이름(예: `스포츠`)과 같으면 존의 카테고리가 된다.

### 4.2 수직동선(`vertical_link`)

| 키 | 필수 | 내용 |
|---|---|---|
| `pattern` | 예 | 대문자로 바꾼 문자에서 찾는 정규식. `id` 그룹이 있으면 그 부분을 ID로 쓴다. |
| `format` | | ID 형식 |
| `map` | | 도면 ID → 표준 ID. 예: `E/S-A: ESC-A` |
| `types` | | ID 접두어(첫 `-` 앞) → 종류(`escalator`, `elevator`, `stair`). 기본값 `ESC`, `EV`, `ST`에 더한다. |
| `words` | | 이름 → 종류. 기본값(`에스컬레이터`, `승강기`, `계단` 등)에 더한다. |

수직동선 ID는 모든 층에서 같아야 층간 연결을 계산할 수 있다. 도면마다 표기가 다르면 `map`으로 표준 ID에 맞춘다.

### 4.3 출입구(`opening`)

`pattern`(필수), `format`, `map`을 수직동선과 같은 방식으로 쓴다. 예: `pattern: '출입구\s*(?P<num>B?\d+)'`, `format: 'ENT-{num}'`. ID가 `ENT`로 시작하거나 이름에 `출입`·`정문`·`입구`가 있으면 출입구(entrance)로 본다.

### 4.4 공실(`vacancy`)

| 키 | 내용 |
|---|---|
| `words` | 공실 표기 목록. 기본 표기(`공실`, `VACANT`, `임대중`, `임대예정`)에 더한다. 괄호로 감싼 표기와 단어 사이 공백도 받는다. |
| `pattern` | 공실 표기 전체 정규식. 있으면 기본 표기와 `words`를 쓰지 않는다. |

## 5. 레이어와 우선순위

레이어 의미는 다음 순서로 정한다. 앞의 것이 이긴다.

1. 매니페스트 `layers_by_floor`(`{층: {의미: 레이어 이름}}`). 사람이 검토 큐에서 고친 층별 매핑이 여기에 저장된다.
2. 매니페스트 `layers`
3. 프로필 `layers`
4. 휴리스틱과 LLM(cad-pipeline.md 2절)

같은 레이어 이름이 매니페스트와 프로필에서 다른 의미면 매니페스트를 따른다.

## 6. 검증

프로필을 올리면 다음을 검사한다. 오류가 하나라도 있으면 거부한다.

| 종류 | 검사 |
|---|---|
| 오류 | 객체가 아님, `profile_id`·`title` 없음, `profile_id` 형식 위반, 알 수 없는 레이어 의미, 한 레이어 이름이 여러 의미에 연결됨, 정규식 문법 오류, 빈 문자열과도 맞는 정규식, 500자 넘는 정규식, `format`에 쓴 자리표시자가 패턴 그룹에 없음, `{이름}` 밖의 중괄호, `zone`에 `code` 그룹과 `format`이 모두 없음, 알 수 없는 수직동선 종류, 알 수 없는 단위, `profile_id`가 `default` |
| 경고 | 알 수 없는 키, `unit_code.format` 없이 `floor` 그룹을 씀, `vacancy.pattern`과 `words`를 함께 씀 |

## 7. 기본 프로필(`default`)

| 항목 | 값 |
|---|---|
| 매장 코드 | `(?<![A-Za-z0-9])(?:B\d+\|\d+F)-[NSEWCP]\d{2}[A-Z]?M?(?![A-Za-z0-9])`. 찾은 문자를 그대로 쓴다. |
| 층 없는 코드 | `^[NSEWCP]\d{2}[A-Z]?M?$` → `{도면 층}-{코드}` |
| 표준 형식 | `^(B\d\|\d+F)-[NSEWCP]\d{2}[A-Z]?M?$` |
| 존 | `^\s*(?P<code>(?:(?:B\d+\|\d+F)-)?Z\d+)(?![A-Za-z0-9])\s*(?P<name>.*)$` |
| 수직동선 | `(?P<id>(?:ESC\|EV\|ST)-[A-Z0-9]{1,3})` |
| 출입구 | `(?P<id>(?:ENT\|DOOR\|GATE\|EXIT)-[A-Z0-9]{1,4})` |
| 공실 | `공실`, `VACANT`, `임대중`, `임대예정`(괄호 허용, 대소문자 무시) |
| 레이어·단위 | 없음. 매니페스트와 휴리스틱을 쓴다. |

`default`는 프로필 기능을 넣기 전의 파서와 같은 결과를 낸다.

## 8. 예시

`data/samples/uploads/profiles/customer_style.yaml`은 다음 규약의 합성 예시 도면(`customer_style_6F.zip`)을 읽는다. 도면 헤더에 단위가 없고 좌표는 mm다. 레이어 이름은 한글이다. 매장 라벨은 `6층 C-05`와 다음 줄의 브랜드명이다.

```yaml
profile_id: customer_style
title: 고객사 스타일 도면(예시)
units: mm
layers:
  unit: [매장_경계]
  unit_label: [매장_명칭]
  walkway: [보행통로]
unit_code:
  pattern: '(?P<floor>지하\s*\d+\s*층|\d+\s*층)\s*(?P<band>[NSEWCP])-(?P<num>\d{2})(?P<suffix>[A-Z]?M?)(?![A-Za-z0-9])'
  bare: '(?P<band>[NSEWCP])-(?P<num>\d{2})(?P<suffix>[A-Z]?M?)'
  format: '{floor}-{band}{num}{suffix}'
zone:
  pattern: '^\s*(?P<num>\d+)\s*구역\s*(?P<name>.*)$'
  format: '{floor}-Z{num}'
vertical_link:
  pattern: '(?P<id>E/S-[A-Z0-9]{1,3}|E/V-[A-Z0-9]{1,3}|계단실-[A-Z0-9]{1,3})'
  map: {E/S-A: ESC-A, E/V-1: EV-1, 계단실-1: ST-1}
  types: {E/S: escalator, E/V: elevator, 계단실: stair}
opening:
  pattern: '출입구\s*(?P<num>B?\d+)'
  format: 'ENT-{num}'
vacancy:
  words: [빈 매장]
```

| 도면 표기 | 표준 코드 |
|---|---|
| `6층 C-05` | `6F-C05` |
| `지하1층 S-02A` | `B1-S02A` |
| `C-05`(6층 도면, 첫 단어) | `6F-C05` |
| `1구역 스포츠` | `6F-Z1`, 존 이름 `스포츠` |
| `E/S-A` | `ESC-A`(escalator) |
| `출입구1 정문` | `ENT-1`, 이름 `정문` |

같은 도면을 `default`로 읽으면 매장 코드를 하나도 찾지 못한다. 업로드 작업은 매장이 없는 층이 있으면 실패하고, 프로필과 레이어 매핑을 확인하라고 알린다.

예시 파일은 `uv run --no-sync python -m cad2anything.mockgen.upload_samples --root data/workspace --out data/samples/uploads`로 다시 만든다.

## 9. 한계

- 프로필은 문자 표기와 레이어 이름만 바꾼다. 도형 규약(매장 경계를 닫힌 폴리라인으로 그리는 것 등)은 바꾸지 않는다.
- 정규식은 Python `re` 문법이다. 줄바꿈이 있는 MTEXT 라벨은 줄바꿈이 `\n`으로 들어온다. 존 패턴에서 줄바꿈을 넘어 맞추려면 `(?s)`를 붙인다.
- 집기 블록 이름과 속성 규칙은 프로필에 없다. 집기 유형은 블록명·`TYPE` 속성 키워드로 판정한다.
