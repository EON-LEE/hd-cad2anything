# Atlas API 계약

Atlas 웹앱과 백엔드(`atlas-api`, FastAPI) 사이의 계약이다. 같은 오리진의 `/api` 아래에서 제공한다. 좌표는 로컬 미터 좌표이며 GeoJSON 형식을 따르지만 위경도가 아니다.

## 1. REST

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/api/meta` | `{facility, revisions:[{rev_id, kind, parent_rev_id, effective_date}], floors:[{code, ordinal, name}], categories:[{code, name_ko, color}]}` |
| GET | `/api/floors/{rev}/{floor}?include=fixtures` | GeoJSON FeatureCollection. feature `id`=entity_id, `properties`={kind, unit_code, brand_id, brand_name, category, vacant, area_m2, zone_code, link_id, link_type, amenity_type, fixture_type} |
| GET | `/api/changes/{rev_from}/{rev_to}` | `{change_set, impact:[{generator, title_ko, risk_tier, approval_mode, floors, reasons}]}` |
| GET | `/api/artifacts/{rev}` | `[{generator, title_ko, status, approval_mode, files:[{path, url}], summary, issues}]` |
| GET | `/api/artifacts/{rev}/{generator}/{file}` | 파일 본문(svg, xlsx, csv) |
| POST | `/api/artifacts/{rev}/{generator}/review` | 본문 `{decision:"approve"\|"reject", reviewer, note}` → 갱신된 artifact |
| POST | `/api/revisions/{rev}/process` | 파싱, diff, 영향 분석, 재생성을 실행하고 요약을 반환한다. |
| GET | `/api/analytics/productivity?rev=&floor=&level=unit&top=` | `{rev, level, floor, period:{start, end, days}, summary, metric:{name, label, min, max}, rows:[{unit_code, floor, zone_code, brand_id, brand_name, category, category_name, area_m2, sales_krw, receipts, aov_krw, sales_per_m2, sales_per_pyeong, gmrof, percentile(0~100), floor_percentile, low_efficiency, driver}], low_efficiency:[...], note}`. 금액은 연환산 전체 규모 값이다. `level`은 `unit`·`zone`·`floor`·`category`. 가상 개정은 404 |
| GET | `/api/analytics/cross-shopping?level=category&focal=&segment=age_band:40s;tier:VIP\|VVIP` | `{level, segment, basket, period, n_baskets, n_customers, caveat, pairs:[{a, b, a_name, b_name, lift, jaccard, confidence_ab, n_customers}]}`. `focal`을 주면 `a`가 focal이다. |
| GET | `/api/analytics/transitions?segment=` | `{floors:[...], matrix:[[...]], top_transitions, fountain:{base_floor, share, customer_days}, shower:{top_floor, share, customer_days}, caveat}` |
| POST | `/api/layout/alternatives` | 본문 `{rev, floors, constraints:{keep_anchors, area_tolerance, max_moves, n_alternatives, movable_categories, allow_cross_floor, save}}` → `{rev, period, label, caveats, alternatives:[{id, rev_id, summary, moves:[{brand_id, brand_name, from_unit, to_unit, delta, delta_low, delta_high, explanation}], objective_before, objective_after(억원), kpi_delta:{sales_krw, low, high}, constraints:[{name, ok, detail}], kpi_by_floor, explanations}]}`. 대안마다 가상 개정 `rev_id`가 저장된다. |
| GET | `/api/analytics/journey?customer_id=&date=&segment=&rank=1&rev=` | 고객 한 명의 하루 쇼핑 동선. 응답은 3절의 `journey` 객체와 같다. `customer_id`·`date`를 생략하면 조건(`segment`)에 맞는 고객 하루 중 `rank`번째를 고른다. `rev`를 주면 그 개정이 실제 매장 배치였던 기간에서만 고른다(가상 개정은 기준 실제 개정의 기간). 해당하는 동선이 없으면 404 |
| GET | `/api/spatial/route?rev=&origin=&destination=&destination_category=&destination_floor=&via=escalator` | 두 지점 사이의 통로망 최단 경로. 응답은 3절의 `route` 객체와 같다. `origin`·`destination`은 매장 코드(`3F-C05`), 출입구 ID(`ENT-1`), 층을 붙인 수직동선·편의시설(`1F:ESC-A`, `8F:RESTROOM`)이다. `destination` 대신 `destination_category`와 `destination_floor`를 주면 그 층에서 걸어서 가장 가까운 해당 카테고리 매장으로 간다. `via`는 `escalator`(기본) 또는 `elevator`. 찾을 수 없는 지점이면 404 |

## 2. 채팅(AG-UI 호환 SSE)

**요청:** `POST /api/chat`

```json
{"thread_id": "optional", "message": "사용자 질문", "ui_state": {"rev": "R2", "floor": "3F", "selection": ["3F-N05"]}}
```

**응답:** `text/event-stream`이다. 각 줄은 `data: {"type": ..., ...}` 형식이다.

| type | 필드 | 의미 |
|---|---|---|
| `RUN_STARTED` | `thread_id`, `run_id` | 실행 시작 |
| `TEXT_MESSAGE_START` | `message_id` | 어시스턴트 메시지 시작. 한 실행에 메시지는 하나다. |
| `TEXT_MESSAGE_CONTENT` | `message_id`, `delta` | 텍스트 조각. 모델이 생성하는 대로 스트리밍한다. |
| `TEXT_MESSAGE_END` | `message_id` | 메시지 종료 |
| `TOOL_CALL_START` | `tool_call_id`, `tool_name`, `args`, `label`, `parent_tool_call_id?`, `agent?` | 도구 호출 시작. `label`은 사용자에게 보여 줄 한국어 설명이다. 예: "4층 공간 생산성 계산" |
| `TOOL_CALL_END` | `tool_call_id`, `summary`, `label?`, `ok?` | 도구 호출 종료. `label`이 있으면 시작 때의 설명을 이 값으로 바꾼다. `summary`는 개발자용 원문이다. |
| `STATE_DELTA` | `delta` | UI 상태에 적용할 JSON Patch(RFC 6902) 배열 |
| `RUN_FINISHED` | `thread_id`, `run_id` | 실행 종료 |
| `RUN_ERROR` | `message` | 오류 |

**화면 표시 원칙:** 채팅은 대화처럼 보여야 한다. 도구 이름, 인자, `summary` 원문은 기본 화면에 보이지 않는다. 실행 중에는 진행 중인 `label` 한 줄만 보이고, 끝나면 단계 목록을 접어 둔다.

## 3. UI 상태 모델

에이전트의 UI 함수 도구 호출은 백엔드가 `STATE_DELTA`로 바꿔 보낸다. UI 함수 도구는 `ui_set_floor`, `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_focus`, `ui_compare`, `ui_show_panel`, `ui_show_journey`, `ui_show_route`, `ui_clear`이다.

```json
{
  "rev": "R2",
  "compareRev": null,
  "floor": "3F",
  "layers": {"fixtures": false, "walkways": true, "labels": true},
  "heatmap": {"metric": "sales_per_m2", "label": "평효율(원/㎡)", "values": {"3F-N05": 0.82}},
  "highlights": [{"unit_code": "3F-N05", "color": "#e53935", "reason": "저효율 하위 10%"}],
  "edges": [{"from": "3F-N05", "to": "2F-C03", "weight": 2.4, "label": "lift 2.4"}],
  "focus": {"unit_codes": ["3F-N05"]},
  "panel": {"type": "changes", "payload": {}},
  "journey": null,
  "route": null
}
```

**`journey`(고객 한 명의 하루 쇼핑 동선)**

```json
{
  "title": "40대 여성 VIP 고객의 쇼핑 동선",
  "customer": {"id": "C004211", "segment": "40대 여성 VIP"},
  "date": "2025-11-08", "weekday": "토", "rev": "R2",
  "query": {"segment": {"age_band": "40s", "gender": "F", "tier": "VIP"}, "rank": 1, "rev": "R2"},
  "stops": [
    {"order": 1, "time": "11:32", "unit_code": "B1-C05", "floor": "B1", "brand_name": "Delino", "category": "FOOD_DELI",
     "category_name": "델리", "amount_krw": 24000, "gap_min": null, "x": 52.4, "y": 31.0}
  ],
  "segments": [{"floor": "B1", "points": [[52.4, 31.0], [58.0, 31.0]], "from_order": 1, "to_order": 2}],
  "transitions": [{"from_order": 1, "to_order": 2, "from_floor": "B1", "to_floor": "1F", "via": "ESC-A"}],
  "summary": {"stops": 4, "floors": ["B1", "1F", "3F"], "total_krw": 512000, "duration_min": 185, "walk_m": 640},
  "caveat": "결제 기록의 순서입니다. 실제로 걸어간 경로는 알 수 없고, 매장 사이 경로는 통로망 최단 경로로 추정했습니다. 고객 ID는 가명입니다."
}
```

- `stops`의 `x`, `y`는 매장 출입구(통로와 맞닿은 지점) 좌표다.
- `rev`는 구매 날짜에 실제로 쓰인 개정이다. 정류장 매장 코드와 경로는 이 개정의 도면 기준이다. 에이전트가 동선을 보여 줄 때 화면의 개정이 이와 다르면 `/rev`도 함께 바꾼다.
- `query`는 같은 조건의 다음 고객을 요청할 때 쓴다. `rev`가 있으면 그 개정이 쓰인 기간의 고객 하루만 고른 것이다.
- 동선과 다른 분석 표시는 함께 두지 않는다. `ui_show_journey`는 `heatmap`, `highlights`, `edges`, `compareRev`, `route`를 비운다. 이후 답변의 `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_compare`는 `journey`를 `null`로 바꾼다. 같은 답변 안에서 그린 동선은 유지한다.
- `segments`는 통로망 최단 경로로 추정한 이동선이다. 층이 바뀌는 구간은 출발 층에서 에스컬레이터까지, 도착 층에서 에스컬레이터부터 매장까지 두 개의 segment로 나뉜다.
- 화면은 현재 층의 `segments`와 `stops`만 그린다. 다른 층에 들르는 정류장이 있으면 층 탭에 표시한다.

**`route`(두 지점 사이의 길)**

```json
{
  "title": "ENT-1 정문 → Orlix Coffee(8F-C05)",
  "rev": "R2",
  "via_type": "escalator",
  "origin": {"ref": "ENT-1", "kind": "entrance", "label": "ENT-1 정문", "floor": "1F", "unit_code": null, "x": 60.0, "y": 0.4},
  "destination": {"ref": "8F-C05", "kind": "unit", "label": "Orlix Coffee(8F-C05)", "floor": "8F", "unit_code": "8F-C05", "x": 41.0, "y": 52.0},
  "segments": [
    {"floor": "1F", "points": [[60.0, 0.4], [60.0, 30.0], [52.0, 38.0]]},
    {"floor": "8F", "points": [[52.0, 38.0], [41.0, 52.0]]}
  ],
  "transitions": [{"from_floor": "1F", "to_floor": "8F", "via": "ESC-A", "floors": 7, "height_m": 35.0}],
  "walk_m": 85,
  "caveat": "통로망 최단 경로로 계산한 걷는 거리예요. 에스컬레이터를 타는 구간과 층마다 갈아타는 거리는 넣지 않았어요."
}
```

- `origin.kind`, `destination.kind`는 `unit`, `entrance`, `escalator`, `elevator`, `stairs`, `amenity` 중 하나다. `x`, `y`는 지점의 출입 지점(통로와 맞닿은 곳) 좌표다.
- `segments`는 층마다 하나다. 출발 층은 출발 지점에서 수직동선까지, 도착 층은 수직동선에서 도착 지점까지다. 같은 층이면 segment가 하나이고 `transitions`는 빈 배열이다.
- `walk_m`은 출발 층과 도착 층에서 걷는 거리의 합이다(5m 단위 반올림). 수직 이동은 `transitions`의 `floors`(지나는 층 수)와 `height_m`(층 높이 합)으로 따로 보여 준다.
- 화면은 현재 층의 segment와, 현재 층에 있는 출발·도착 지점을 그린다. 경로가 지나는 다른 층은 층 탭에 표시한다.
- 경로도 동선처럼 단독으로 표시한다. `ui_show_route`는 `heatmap`, `highlights`, `edges`, `compareRev`, `journey`를 비우고, `ui_show_journey`는 `route`를 비운다. 이후 답변의 `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_compare`는 `journey`와 `route`를 `null`로 바꾼다. 같은 답변 안에서 그린 경로는 유지한다.

| 필드 | 규칙 |
|---|---|
| `heatmap`, `heatmap.values` | `values`는 0~1로 정규화한 값이다. `heatmap`이나 `heatmap.values`가 `null`이면 heatmap을 끄고 카테고리 색으로 칠한다. |
| `heatmap.legend` | 선택 필드 `{low, high, note, binary}`. 색의 의미가 "값이 클수록 진함"이 아닐 때 범례 문구를 지정한다. `binary`가 `true`이면 범례에 두 색만 표시한다. 예: 저효율 플래그, 에스컬레이터 접근성(가까울수록 진함). |
| `edges` | 다른 층 매장과 이으면 화면 가장자리의 층 배지로 연결해 표시한다. |
| `focus` | 다른 층 매장에 초점을 맞출 때는 `/floor` 변경을 같은 `STATE_DELTA`에 함께 보낸다. |
| `panel.type` | `changes`, `artifact`, `layout`, `table`, `journey`, `none` 중 하나다. `journey`는 `journey` 상태의 동선 목록을 연다. |
| `panel.payload` | `changes`: `{rev_from, rev_to}`, `artifact`: `{generator}`, `layout`: `{rev, floors, alternatives}`, `table`: `{title, columns, rows}`. 백엔드는 `table`의 객체 행과 열 단위 데이터를 값 배열 행으로 바꾼다. 행이 없으면 패널을 열지 않고 에이전트에게 오류를 돌려준다. |

**표기 규칙**

- SSE는 `data: {...}` 다음에 빈 줄을 두는 표준 형식을 쓴다.
- 필드 이름은 snake_case를 기준으로 한다. 클라이언트는 AG-UI의 camelCase 이름도 받아들이고, 모르는 이벤트 유형은 무시한다.
- `STATE_DELTA`는 전부 적용하거나 전부 버린다. 한 연산이라도 실패하면 화면에 갱신 실패를 표시한다.
- `percentile`은 0~100 척도다.
- 전문 에이전트 안에서 일어난 도구 호출은 `TOOL_CALL_START`에 `parent_tool_call_id`와 `agent`를 붙여 보낸다.
