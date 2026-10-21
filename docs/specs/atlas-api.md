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
| GET | `/api/analytics/productivity?rev=&floor=&level=unit` | `{rows:[{unit_code, brand_name, category, sales_krw, sales_per_m2, sales_per_pyeong, percentile, low_efficiency, driver}], metric:{name, label, min, max}}` |
| GET | `/api/analytics/cross-shopping?level=brand&focal=&segment=age_band:40s;tier:VIP\|VVIP` | `{pairs:[{a, b, a_name, b_name, lift, jaccard, confidence_ab, n_customers}]}` |
| GET | `/api/analytics/transitions?segment=` | `{floors:[...], matrix:[[...]], fountain, shower}` |
| POST | `/api/layout/alternatives` | 본문 `{rev, floors, constraints}` → `{alternatives:[{id, moves:[{brand_id, brand_name, from_unit, to_unit}], objective_before, objective_after, kpi_delta:{sales_krw, low, high}, constraints:[{name, ok}], explanations:[...]}]}` |

## 2. 채팅(AG-UI 호환 SSE)

**요청:** `POST /api/chat`

```json
{"thread_id": "optional", "message": "사용자 질문", "ui_state": {"rev": "R2", "floor": "3F", "selection": ["3F-N05"]}}
```

**응답:** `text/event-stream`이다. 각 줄은 `data: {"type": ..., ...}` 형식이다.

| type | 필드 | 의미 |
|---|---|---|
| `RUN_STARTED` | `thread_id`, `run_id` | 실행 시작 |
| `TEXT_MESSAGE_START` | `message_id` | 어시스턴트 메시지 시작 |
| `TEXT_MESSAGE_CONTENT` | `message_id`, `delta` | 텍스트 조각 |
| `TEXT_MESSAGE_END` | `message_id` | 메시지 종료 |
| `TOOL_CALL_START` | `tool_call_id`, `tool_name`, `args` | 에이전트가 도구를 호출함. 투명성 표시용 |
| `TOOL_CALL_END` | `tool_call_id`, `summary` | 도구 결과 요약 |
| `STATE_DELTA` | `delta` | UI 상태에 적용할 JSON Patch(RFC 6902) 배열 |
| `RUN_FINISHED` | `thread_id`, `run_id` | 실행 종료 |
| `RUN_ERROR` | `message` | 오류 |

## 3. UI 상태 모델

에이전트의 UI 함수 도구 호출은 백엔드가 `STATE_DELTA`로 바꿔 보낸다. UI 함수 도구는 `ui_set_floor`, `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_focus`, `ui_compare`, `ui_show_panel`이다.

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
  "panel": {"type": "changes", "payload": {}}
}
```

| 필드 | 규칙 |
|---|---|
| `heatmap.values` | 0~1로 정규화한 값이다. `null`이면 heatmap을 끄고 카테고리 색으로 칠한다. |
| `edges` | 다른 층 매장과 이으면 화면 가장자리의 층 배지로 연결해 표시한다. |
| `panel.type` | `changes`, `artifact`, `layout`, `table`, `none` 중 하나다. |
