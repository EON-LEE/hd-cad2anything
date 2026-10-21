# CAD 규약 (합성 도면 ↔ CAD 이해 파이프라인)

이 문서는 합성 도면 생성기(`cad2anything.mockgen`)와 CAD 이해 파이프라인(`cad2anything.cadcore`)이 함께 따르는 계약이다. 실제 고객 도면은 이 규약을 따르지 않을 수 있다. 그래서 파이프라인은 레이어 이름에만 의존하지 않고 내용 기반 추론과 LLM 보조 해석을 함께 쓴다.

## 1. 좌표와 단위

| 항목 | 규약 |
|---|---|
| 좌표계 | 로컬 공학 좌표계. 원점은 건물 남서쪽 모서리이고, 모든 층이 같은 원점을 공유한다. |
| 건물 외곽 | 120 m × 80 m 직사각형(`catalog.BUILDING_WIDTH_M`, `BUILDING_DEPTH_M`) |
| 층고 | 5 m |
| clean 변형 | `$INSUNITS=6`(m), DXF R2018(AC1032), UTF-8 |
| messy 변형 | `$INSUNITS=4`(mm), 좌표 ×1000, DXF R2000(AC1015), `$DWGCODEPAGE=ANSI_949`(CP949) |

## 2. 파일 구성

| 항목 | 규약 |
|---|---|
| 파일 단위 | 개정과 변형별로 층마다 DXF 파일 1개 |
| 파일명 | `flagship_{rev}_{floor}.dxf`. 예: `flagship_R1_3F.dxf` |
| 매니페스트 | 각 폴더에 `manifest.json`을 둔다. |

**매니페스트 필드**

| 필드 | 내용 |
|---|---|
| `facility` | 시설 코드 |
| `revision` | 개정 코드 |
| `kind` | `actual` 또는 `hypothetical` |
| `parent_revision` | 부모 개정 코드 |
| `effective_date` | 적용일 |
| `variant` | `clean` 또는 `messy` |
| `units` | `m` 또는 `mm` |
| `levels[]` | 항목마다 `floor`, `ordinal`, `file`, `elevation_m`, `height_m` |
| `layers` | 의미 → 레이어 이름 매핑. **clean 변형에만 있다.** |

## 3. 표준 레이어(clean)

| 의미 | 레이어 | 엔티티 |
|---|---|---|
| exterior | `A-EXTR` | 닫힌 LWPOLYLINE(건물 외곽) |
| wall | `A-WALL` | LINE / LWPOLYLINE |
| unit | `A-UNIT` | 닫힌 LWPOLYLINE(매장 경계) |
| unit_label | `A-UNIT-IDEN` | MTEXT `"{unit_code}\P{brand_name}"`. 공실은 `"{unit_code}\P(공실)"` |
| zone | `A-ZONE` | 닫힌 LWPOLYLINE |
| zone_label | `A-ZONE-IDEN` | MTEXT `"{zone_code} {zone_name}"` |
| walkway | `A-CORR` | 닫힌 LWPOLYLINE(보행 통로 폴리곤) |
| escalator | `A-VERT-ESCL` | 닫힌 LWPOLYLINE + TEXT `"ESC-A 에스컬레이터"` |
| elevator | `A-VERT-ELEV` | 닫힌 LWPOLYLINE + TEXT `"EV-1 엘리베이터"` |
| stair | `A-VERT-STAI` | 닫힌 LWPOLYLINE + TEXT `"ST-1 계단"` |
| opening | `A-DOOR` | 외곽 위 LINE + TEXT `"ENT-1 정문"`(출입구) |
| amenity | `A-AMEN` | 닫힌 LWPOLYLINE |
| amenity_label | `A-AMEN-IDEN` | TEXT `"WC 화장실"`, `"INFO 안내데스크"`, `"ATM"`, `"NURSING 수유실"`, `"LOUNGE 고객라운지"` |
| fixture | `A-FIXT` | INSERT. 블록명 `FX_{TYPE}`, 속성 `FIXTURE_ID`, `TYPE`, `W`, `D` |
| annotation | `A-ANNO` | 도면 제목 등 |

**수직 연결 ID:** 수직동선 ID(`ESC-A`, `ESC-B`, `EV-1`, `EV-2`, `ST-1`, `ST-2`)는 모든 층에서 같은 좌표와 같은 ID를 쓴다. 층간 연결은 이 ID로 맞춘다.

## 4. 코드 체계

| 대상 | 규약 |
|---|---|
| 매장 코드 | `{floor}-{band}{nn}`. band는 `N`(북측 외곽), `S`(남측 외곽), `E`(동측), `W`(서측), `C`(중앙 섬), `P`(팝업)이다. 예: `3F-N05` |
| 분할 | 원래 코드에 `A`/`B`를 붙인다. 예: `6F-E01A`, `6F-E01B` |
| 병합 | 첫 코드에 `M`을 붙인다. 예: `3F-S07M` |
| 존 코드 | `{floor}-Z{n}` |
| 집기 ID | `{unit_code}-F{nnn}` |
| 브랜드 ID | `BR{nnnn}`. 브랜드명은 모두 가상이다. |

## 5. messy 변형 규칙

messy 변형은 실제 도면의 지저분함을 흉내 낸다. 파이프라인은 이 규칙 목록 자체를 알지 못한다고 가정한다.

| 대상 | 규칙 |
|---|---|
| 레이어 이름 | 층마다 한글·영문 동의어 풀에서 무작위로 고른다. 예: `벽`, `WALL`, `A-WALL-EXST`, `Wall_구조`, `매장경계`, `TENANT`, `구획`, `통로`, `집기` |
| 의미 없는 이름 | 일부 층의 일부 의미는 `0`, `LAYER_17` 같은 이름을 쓴다. |
| 매장 경계 | 약 70%는 닫힌 LWPOLYLINE이다. 약 20%는 끝점 간격 10~40 mm인 열린 LWPOLYLINE이다. 약 10%는 LINE 4개로 그린다. |
| 라벨 | TEXT를 쓴다. 매장 코드와 브랜드명을 하나의 TEXT로 쓰거나, 중심점 근처에 따로 쓴다. |
| 집기 블록 | 블록 이름은 한글이다(`쇼케이스`, `행거_01` 등). 일부 블록에는 속성이 없다. |
| 잡음 | 치수, 메모 같은 잡음 엔티티를 무관한 레이어에 섞는다. |

## 6. 정답(ground truth)

| 항목 | 내용 |
|---|---|
| 배치 정답 | 개정마다 `layout_{rev}.json`을 둔다. 형식은 `SpatialModel.to_dict()`이다. |
| 변경 정답 | 연속 개정 사이의 `changes_{from}_{to}.json`을 둔다. 형식은 `ChangeSet.to_dict()`이다. |
| 엔티티 ID | 같은 물리 공간이면 개정이 바뀌어도 같은 ID를 유지한다. 예: `U-3F-N05`. 브랜드가 이동하면 공간 ID는 그대로이고 `brand_id` 속성이 바뀐다. |
