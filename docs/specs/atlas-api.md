# Atlas API 계약

Atlas 웹앱과 백엔드(`atlas-api`, FastAPI) 사이의 계약이다. 같은 오리진의 `/api` 아래에서 제공한다. 좌표는 로컬 미터 좌표이며 GeoJSON 형식을 따르지만 위경도가 아니다.

## 1. REST

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/api/meta` | `{facility, data_through, revisions:[{rev_id, kind, parent_rev_id, effective_date, source, notes, model_source, has_sales}], floors:[{code, ordinal, name}], categories:[{code, name_ko, color}]}`. `data_through`는 거래 데이터의 마지막 날짜다. `has_sales`는 실제 개정의 적용 기간에 거래 실적이 있는지다. 적용일이 `data_through` 뒤인 실제 개정(예: 다음 시즌용으로 올린 도면)은 `false`이고, 분석 값은 가상 개정처럼 추정치로 제공된다. 화면의 기본 개정은 `has_sales`가 `true`인 가장 최근 실제 개정이다. `model_source`는 모델 출처(`parsed`, `truth`, `uploaded`, `hypothetical`)이고, `source`는 개정을 만든 도구 설명 문자열이다. |
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

### 1.1 시즌 개편(relayout)

개편 브리프는 사람이 만든 메타데이터다. 시즌, 기준 개정, 대상 층, 목적, 입점·퇴점 브랜드를 담는다. 브리프는 `relayout_brief` 메타데이터로 올린다(1.2절). 백엔드는 브리프마다 개편 프로젝트(진단 → 대안 → 선택 → 도면 대조 → 보고서)를 저장한다.

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/api/relayout/briefs` | `[{brief_id, title, season, year, effective_date, base_rev, floors, objective, status, entries, exits, selected_alt_id, updated_at}]`. `entries`·`exits`는 건수. `status`는 `draft`, `diagnosed`, `planned`, `selected`, `checked` 중 하나다. 브리프 파일에 오류가 있으면 `{brief_id, title, status: "invalid", error}`만 준다. |
| GET | `/api/relayout/{brief_id}` | 프로젝트(아래). 없는 브리프면 404 |
| POST | `/api/relayout/{brief_id}/diagnose` | 본문 없음 → `diagnosis` |
| POST | `/api/relayout/{brief_id}/plan` | 본문 `{objective?: "sales"\|"commission", n_alternatives?: 1~5, max_moves?, downtime_weeks?, movable?: "expiring"\|"all", extra_exits?: [브랜드 ID나 이름], extra_entries?: [브랜드 ID나 이름]}`(생략하면 브리프 값) → `plan`. 진단이 없으면 먼저 진단한다. 수십 초 걸릴 수 있다. |
| POST | `/api/relayout/{brief_id}/select` | 본문 `{alt_id, decided_by}` → 프로젝트. 사람이 고른 대안을 기록한다. |
| POST | `/api/relayout/{brief_id}/check` | 본문 `{rev}`(CAD팀이 그려 올린 실제 개정) → `check`. 선택한 대안이 없으면 400 |
| GET | `/api/relayout/{brief_id}/report` | 개편 제안서(`text/html`). 진단, 대안 비교, 선택안, 도면 대조, 가정과 한계를 담는다. |

**프로젝트(`GET /api/relayout/{brief_id}`)**

```json
{
  "brief": {
    "brief_id": "FW2026", "title": "2026 F/W MD 개편", "season": "FW", "year": 2026, "effective_date": "2026-10-01",
    "base_rev": "R2", "floors": ["4F", "5F", "6F"], "objective": "commission", "max_moves": 6, "downtime_weeks": 2,
    "movable": "expiring", "keep_anchors": true,
    "entries": [{"brand_id": "BR0470", "brand_name": "...", "category": "OUTDOOR", "category_name": "아웃도어",
                 "target_area_m2": [150, 220], "preferred_floors": ["6F"], "commission_rate": 0.28, "note": "..."}],
    "exits": [{"brand_id": "BR0327", "brand_name": "Ravandre Nuit", "unit_code": "4F-E05", "reason": "계약 만료·저효율"}],
    "notes": "..."
  },
  "status": "selected",
  "diagnosis": {}, "plan": {}, "selected_alt_id": "ALT-1", "decided_by": "MD팀 김OO", "decided_at": "2026-10-23T10:00:00+09:00",
  "check": null, "report_url": "/api/relayout/FW2026/report", "updated_at": "2026-10-23T10:00:00+09:00"
}
```

- `diagnosis`, `plan`, `check`는 아직 만들지 않았으면 `null`이다. `report_url`은 항상 있다. 제안서는 요청할 때 그 시점의 프로젝트로 만든다.

**`diagnosis`**

```json
{
  "season": {"code": "FW", "label": "F/W(9~2월)", "year": 2026, "last": {"start": "2025-10-01", "end": "2026-02-28"},
             "previous": {"start": "2024-10-01", "end": "2025-02-28"}},
  "season_factors": [{"category": "OUTDOOR", "category_name": "아웃도어", "factor": 1.21}],
  "floors": [{"floor": "4F", "sales_per_m2": 7500000, "yoy_pct": 3.2, "low_efficiency": 5}],
  "candidates": [{"unit_code": "4F-E05", "floor": "4F", "brand_id": "BR0101", "brand_name": "Ravandre Nuit", "category_name": "란제리",
                  "lease_end": "2026-09-30", "sales_per_m2": 4030760, "percentile": 2.1, "low_efficiency": true, "reasons": ["계약 만료", "저효율"]}],
  "vacancies": [{"unit_code": "5F-W03", "floor": "5F", "area_m2": 190.8}],
  "entries": [{"brand_id": "NB01", "brand_name": "Norvik Outdoor", "category_name": "아웃도어", "target_area_m2": [150, 220],
               "preferred_floors": ["6F"], "feasible_units": 4}],
  "exits": [{"brand_id": "BR0101", "brand_name": "Ravandre Nuit", "unit_code": "4F-E05", "reason": "계약 만료·저효율"}],
  "notes": ["..."]
}
```

- `season_factors`: 카테고리의 해당 시즌 일평균 매출을 연간 일평균 매출로 나눈 값이다. 1보다 크면 그 시즌에 강하다.
- `season.last`, `season.previous`: 최근 같은 시즌과 1년 전 같은 시즌이다. 두 해 모두 데이터가 있는 날짜 범위로 맞춘다.
- `floors.yoy_pct`: 1년 전 같은 시즌과 비교한 일평균 매출 증감률이다.
- `candidates`: 대상 층에서 계약이 개편 적용일 전에 끝나거나 저효율인 매장이다.
- `entries.feasible_units`: 공실과 퇴점 매장 중 면적·선호 층 조건에 맞는 매장 수다.

**`plan`**

```json
{
  "params": {"objective": "commission", "season": "FW", "max_moves": 6, "downtime_weeks": 2, "movable": "expiring", "floors": ["4F", "5F", "6F"],
             "extra_exits": [], "extra_entries": []},
  "current": {"sales_krw": 0, "objective_krw": 0},
  "baseline": {"sales_krw": 0, "objective_krw": 0, "note": "퇴점·입점만 반영한 기준안"},
  "alternatives": [{
    "alt_id": "ALT-1", "rev_id": "H8", "summary": "...",
    "kpi": {"sales_delta_krw": 0, "sales_low_krw": 0, "sales_high_krw": 0, "objective_delta_krw": 0, "move_cost_krw": 0, "net_krw": 0,
            "vs_current_sales_krw": 0, "moves": 3},
    "changes": [{"type": "move", "brand_id": "BR0042", "brand_name": "...", "category_name": "...", "from_unit": "4F-C10",
                 "to_unit": "4F-C05", "from_floor": "4F", "to_floor": "4F", "delta_krw": 0, "explanation": "..."}],
    "constraints": [{"name": "앵커 고정", "ok": true, "detail": "..."}],
    "explanations": ["이웃 매장 인접 친화도 변화에 따른 파급 효과: ..."]
  }],
  "unplaced": [{"brand_id": "BR0470", "brand_name": "...", "reason": "면적·선호 층에 맞는 빈 매장이 없습니다."}],
  "caveats": ["..."]
}
```

- `changes.type`은 `move`(이전), `entry`(입점, `from_unit` 없음), `exit`(퇴점, `to_unit` 없음) 중 하나다. 변경마다 `explanation`(근거)이 있다.
- `explanations`는 변경 하나에 속하지 않는 참고 설명이다. 예: 입점 브랜드 위치 조정, 이웃 매장의 인접 친화도 파급 효과.
- `current`는 지금 배치, `baseline`은 퇴점·입점만 반영하고 기존 브랜드를 옮기지 않은 배치다. 대안의 `kpi`는 `baseline` 대비 변화다. `vs_current_sales_krw`는 지금 배치 대비 기대 매출 변화다.
- `objective_krw`는 시즌 가중치를 곱한 기대 매출이다. `objective=commission`이면 여기에 수수료율도 곱한다.
- `move_cost_krw`는 이전하는 기존 브랜드의 공사 기간(`downtime_weeks`) 영업 손실 추정이다. 입점 브랜드에는 붙지 않는다.
- `net_krw = objective_delta_krw - move_cost_krw`이다.
- `unplaced`는 넣을 자리를 찾지 못한 입점 브랜드다.
- 금액은 모두 추정치이며 연 단위다. 대안마다 가상 개정 `rev_id`가 저장된다. 화면은 `rev=rev_id`, `compareRev=base_rev`로 대안을 도면에 보여 줄 수 있다.

**`check`(계획안과 실제 도면 대조)**

```json
{"rev": "R3", "plan_rev": "H8", "alt_id": "ALT-1", "conformance_pct": 75.0,
 "items": [{"type": "move", "type_label": "이전", "brand_id": "BR0042", "brand_name": "...", "from_unit": "4F-C10", "planned_unit": "4F-C05",
            "actual_unit": "4F-C06", "status": "different_unit", "status_label": "다른 매장"}],
 "unexpected": [{"unit_code": "5F-N02", "before_brand": "BR0333", "before_brand_name": "...", "after_brand": "BR0166", "after_brand_name": "..."}],
 "area_changes": [{"unit_code": "4F-C05", "before_m2": 120.0, "after_m2": 127.2, "delta_pct": 6.0}],
 "summary": "..."}
```

- `items.status`는 `match`, `different_unit`, `missing`, `not_exited` 중 하나다.

### 1.2 도면·메타데이터 업로드

사람이 만든 도면(DXF)과 메타데이터를 올리면 백엔드가 검증하고 처리한다. 처리 결과는 작업공간과 미러에 저장한다.

| 메서드 | 경로 | 응답 |
|---|---|---|
| POST | `/api/uploads/drawings` | multipart: `files`(DXF 여러 개 또는 ZIP 1개), `rev_id`, `parent_rev?`(기본: 실적이 있는 가장 최근 실제 개정), `effective_date`(YYYY-MM-DD), `units?`(`m`·`mm`, 기본: 프로필 값), `profile?`(도면 규약 프로필 ID, 기본 `default`), `run_generators?`(`true`·`false`, 기본 `true`) → `202 {job_id, status}`. 형식 오류는 400, 같은 ID의 기존 개정(업로드로 만든 개정 제외)은 409 |
| GET | `/api/uploads/jobs` | 최근 작업 목록(최신순, 최대 20개) `[{job_id, kind, status, rev_id, created_at, updated_at}]` |
| GET | `/api/uploads/jobs/{job_id}` | 작업 상태와 결과(아래). 없으면 404 |
| POST | `/api/uploads/jobs/{job_id}/layer-mapping` | 본문 `{mappings: [{layer, semantic, floor?}]}` → 매핑을 저장하고 다시 처리한다. 응답은 작업 상태다. |
| GET | `/api/uploads/profiles` | 도면 규약 프로필 목록 `[{profile_id, title, description, units}]`. `default`가 항상 있다. |
| POST | `/api/uploads/metadata` | multipart: `kind`(`brands`, `tenancies`, `leases`, `pos_terminals`, `relayout_brief`, `drawing_profile`), `file`(CSV, XLSX, YAML, JSON) → `{kind, status: "accepted"\|"rejected", rows, columns, errors, warnings, backup, id?}`. `id`는 브리프·프로필의 ID다. 거부해도 200으로 응답하고 기존 데이터는 바꾸지 않는다. |
| GET | `/api/uploads/metadata` | 종류별 현재 상태 `[{kind, present, rows, updated_at, required_columns, items}]`. `items`는 브리프·프로필의 ID 목록(표 종류는 빈 배열)이다. |

**도면 파일 규칙**

- 파일 이름에 층 코드가 있어야 한다. 예: `R3_4F.dxf`, `6F.dxf`, `B1.dxf`, `4층.dxf`. 같은 층 파일이 둘이면 400이다.
- 파일 하나는 30MB, 전체는 100MB까지 받는다. ZIP 안의 DXF가 아닌 파일은 무시한다.
- `rev_id`는 영문 대문자·숫자·`-`·`_`로 1~16자다.
- 작업은 한 번에 하나씩 처리한다. 앞 작업이 있으면 `queued`로 기다린다.

**메타데이터 종류**

| `kind` | 형식 | 필수 열·키 | 저장 |
|---|---|---|---|
| `brands` | CSV·XLSX | `brand_id`, `brand_name`, `category` | 표 전체 교체 |
| `tenancies` | CSV·XLSX | `brand_id`, `unit_code`, `rev_id`, `valid_from`, `valid_to` | 표 전체 교체 |
| `leases` | CSV·XLSX | `lease_id`, `brand_id`, `unit_code`, `lease_start`, `lease_end` (선택: `contract_area_m2`, `commission_rate`) | 표 전체 교체 |
| `pos_terminals` | CSV·XLSX | `pos_id`, `brand_id`, `unit_code`, `valid_from`, `valid_to` | 표 전체 교체 |
| `relayout_brief` | YAML·JSON | `brief_id`, `title`, `season`(`SS`·`FW`), `year`, `effective_date`, `base_rev`, `floors` | 브리프 하나 추가·교체 |
| `drawing_profile` | YAML·JSON | `profile_id`, `title` | 프로필 하나 추가·교체 |

- 표를 교체하기 전에 이전 파일을 `backup`에 적힌 경로로 보관한다.
- 오류(`errors`)가 하나라도 있으면 거부한다. 경고(`warnings`)만 있으면 받아들인다.

**작업 상태(`GET /api/uploads/jobs/{job_id}`)**

```json
{
  "job_id": "J20261023-0001", "kind": "drawing", "status": "needs_review", "rev_id": "R3", "parent_rev": "R2",
  "effective_date": "2026-10-01", "profile": "default", "created_at": "...", "updated_at": "...",
  "files": [{"name": "R3_4F.dxf", "floor": "4F", "bytes": 381220}],
  "floors_uploaded": ["4F"], "floors_inherited": ["B1", "1F", "2F", "3F", "5F", "6F", "7F", "8F"],
  "steps": [{"name": "parse", "label": "도면 읽기", "status": "done", "message": "매장 50개"}],
  "parse": {"units": 50, "issues": {"error": 0, "warning": 3, "info": 5},
            "review_queue": [{"floor": "4F", "layer": "LAYER_17", "semantic": "unit", "confidence": 0.55,
                              "alternatives": ["walkway"], "samples": ["4F-C05"], "question": "..."}]},
  "changes": {"total": 7, "by_type": {"move": 3, "reassign": 2, "resize": 2}},
  "impact": [{"generator": "area_schedule", "title_ko": "면적표", "risk_tier": "low"}],
  "artifacts": [{"generator": "area_schedule", "status": "draft"}],
  "qa": {"errors": 0, "warnings": 2, "issues": [{"severity": "warning", "code": "area_mismatch", "unit_code": "4F-C05",
                                                  "message": "계약 면적 120.0㎡와 도면 면적 127.2㎡가 6.0% 다릅니다."}]},
  "relayout_check": {"brief_id": "FW2026", "alt_id": "ALT-1", "conformance_pct": 75.0, "summary": "..."},
  "error": null
}
```

- `status`는 `queued`, `running`, `needs_review`, `done`, `failed` 중 하나다. 검토 큐가 비어 있지 않으면 `needs_review`로 끝난다. 이때도 모델은 추정 의미로 만들어진다.
- `steps.name`은 `save`(파일 저장), `parse`(도면 읽기), `merge`(층 합치기), `diff`(변경 비교), `qa`(계약 정보 대조), `impact`(영향 분석), `generate`(산출물 재생성), `relayout_check`(개편안 대조) 순서다. `steps.status`는 `pending`, `running`, `done`, `skipped`, `failed` 중 하나다.
- 올리지 않은 층은 부모 개정에서 그대로 가져온다(`floors_inherited`).
- `qa`는 도면을 계약 정보(`leases`, 적용일에 유효한 계약만)와 대조한 결과다. `code`는 `unit_missing_in_drawing`(계약 매장이 도면에 없음), `unit_without_lease`(도면에 브랜드가 있지만 계약 없음), `brand_mismatch`(도면 브랜드와 계약 브랜드가 다름), `area_mismatch`(계약 면적과 도면 면적이 3% 넘게 다름) 중 하나다.
- `relayout_check`는 이 개정의 부모가 선택된 개편안의 기준 개정이고 적용일이 브리프 적용일과 같으면 자동으로 채워진다. 형식은 1.1절 `check`의 요약이다. 조건에 맞지 않으면 이 단계는 `skipped`다.
- `semantic`은 [CAD 파이프라인](cad-pipeline.md)의 레이어 의미 16개 중 하나다.

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
| `panel.type` | `changes`, `artifact`, `layout`, `table`, `journey`, `uploads`, `none` 중 하나다. `journey`는 `journey` 상태의 동선 목록을 연다. `uploads`는 개정 검토 탭의 업로드 영역을 연다. |
| `panel.payload` | `changes`: `{rev_from, rev_to}`, `artifact`: `{generator}`, `layout`: `{rev, floors, alternatives}` 또는 `{brief_id}`(시즌 개편 프로젝트를 연다), `uploads`: `{job_id?}`(업로드 작업을 연다), `table`: `{title, columns, rows}`. 백엔드는 `table`의 객체 행과 열 단위 데이터를 값 배열 행으로 바꾼다. 행이 없으면 패널을 열지 않고 에이전트에게 오류를 돌려준다. |

**표기 규칙**

- SSE는 `data: {...}` 다음에 빈 줄을 두는 표준 형식을 쓴다.
- 필드 이름은 snake_case를 기준으로 한다. 클라이언트는 AG-UI의 camelCase 이름도 받아들이고, 모르는 이벤트 유형은 무시한다.
- `STATE_DELTA`는 전부 적용하거나 전부 버린다. 한 연산이라도 실패하면 화면에 갱신 실패를 표시한다.
- `percentile`은 0~100 척도다.
- 전문 에이전트 안에서 일어난 도구 호출은 `TOOL_CALL_START`에 `parent_tool_call_id`와 `agent`를 붙여 보낸다.
