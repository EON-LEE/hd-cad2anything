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
| GET | `/api/analytics/productivity?rev=&floor=&level=unit&top=` | `{rev, level, floor, period:{start, end, days}, summary, metric:{name, label, min, max}, rows:[{unit_code, floor, zone_code, brand_id, brand_name, category, category_name, area_m2, sales_krw, receipts, aov_krw, sales_per_m2, sales_per_pyeong, gmrof, percentile(0~100), floor_percentile, low_efficiency, category_index, low_reason, driver}], low_efficiency:[...], category_weak:[...], category_mix:[{category, category_name, n_units, sales_per_m2, vs_scope_pct, low_efficiency}], reading_guide, note}`. 금액은 연환산 전체 규모 값이다. `level`은 `unit`·`zone`·`floor`·`category`. 가상 개정은 404. `category_index`는 같은 카테고리 매장(점포 전체, 3곳 이상) ㎡당 매출 중앙값 대비 비율이다. `low_reason`은 저효율 매장에만 있으며 `카테고리 특성`(지수 0.7 이상) 또는 `같은 카테고리 대비 부진`(0.7 미만)이다. `category_weak`는 지수 0.7 미만 매장, `category_mix`는 범위 안 카테고리별 ㎡당 매출과 범위 전체 대비 차이(%)다. 이 네 필드와 `summary.low_efficiency_by_reason`, `summary.category_weak`는 `level=unit`일 때만 있다. |
| GET | `/api/analytics/cross-shopping?level=category&focal=&segment=age_band:40s;tier:VIP\|VVIP` | `{level, segment, basket, period, n_baskets, n_customers, caveat, pairs:[{a, b, a_name, b_name, lift, jaccard, confidence_ab, n_customers}]}`. `focal`을 주면 `a`가 focal이다. |
| GET | `/api/analytics/transitions?segment=` | `{floors:[...], matrix:[[...]], top_transitions, fountain:{base_floor, share, customer_days}, shower:{top_floor, share, customer_days}, caveat}` |
| POST | `/api/layout/alternatives` | 본문 `{rev, floors, constraints:{keep_anchors, area_tolerance, max_moves, n_alternatives, movable_categories, allow_cross_floor, save}}` → `{rev, period, label, caveats, search:{status, objective_rounding_krw, net_upper_bound_krw, bound_krw?, gap_krw?, note}, alternatives:[{id, rev_id, summary, search_status, objective_rounding_krw, bound_krw?, gap_krw?, moves:[{brand_id, brand_name, from_unit, to_unit, delta, delta_low, delta_high, explanation}], objective_before, objective_after(억원), kpi_delta:{sales_krw, low, high}, constraints:[{name, ok, detail}], kpi_by_floor, explanations}]}`. 대안마다 가상 개정 `rev_id`가 저장된다. `search`·최적성 상태·반올림과 상한의 범위는 1.1절 `plan`의 같은 필드와 같다. |
| GET | `/api/analytics/journey?customer_id=&date=&segment=&rank=1&rev=` | 고객 한 명의 하루 쇼핑 동선. 응답은 3절의 `journey` 객체와 같다. `customer_id`·`date`를 생략하면 조건(`segment`)에 맞는 고객 하루 중 `rank`번째를 고른다. `rev`를 주면 그 개정이 실제 매장 배치였던 기간에서만 고른다(가상 개정은 기준 실제 개정의 기간). 해당하는 동선이 없으면 404 |
| GET | `/api/spatial/route?rev=&origin=&destination=&destination_category=&destination_floor=&via=escalator` | 두 지점 사이의 통로망 최단 경로. 응답은 3절의 `route` 객체와 같다. `origin`·`destination`은 매장 코드(`3F-C05`), 출입구 ID(`ENT-1`), 층을 붙인 수직동선·편의시설(`1F:ESC-A`, `8F:RESTROOM`)이다. `destination` 대신 `destination_category`와 `destination_floor`를 주면 그 층에서 걸어서 가장 가까운 해당 카테고리 매장으로 간다. `via`는 `escalator`(기본) 또는 `elevator`. 찾을 수 없는 지점이면 404 |
| GET | `/api/spatial/flow?mode=traffic&rev=&floor=&segment=&day_type=&hours=&category=&min_width_m=&max_travel_m=` | 통로 위에 그리는 집계 결과. 응답은 3절의 `flow` 객체와 같다. `mode`는 `traffic`(추정 유동), `popup`(팝업 후보), `egress`(통로 폭·피난 거리 점검) 중 하나다. `segment`는 채팅 API와 같은 고객군 문자열(`age_band:40s;tier:VIP`), `day_type`은 `all`·`weekday`·`weekend`, `hours`는 `10-14`처럼 시작·끝 시각이다. 잘못된 인자는 400 |
| GET | `/api/analytics/stores?level=floor&metric=sales_per_m2&floor=&category=` | 다점포 비교. `{level, metric, metric_label, period, stores:[{store_id, store_name, region, format, flagship}], rows:[{key, label, flagship, peer_median, rank, n_stores, gap_pct, values:{store_id: value}}], notes, caveat}`. `level`은 `store`, `floor`, `category` 중 하나다. 대상 점포 실적은 거래에서 계산하고, 다른 점포는 `store_kpis` 메타데이터에서 읽는다. |
| GET | `/api/analytics/relayout-review?rev=R2&window_days=90` | 지난 개편의 성과 추적. `{rev, parent_rev, effective_date, window:{before:{start,end}, after:{start,end}, days}, items:[{change_type, unit_code, floor, brand_name, before_daily_krw, after_daily_krw, change_pct, control_pct, vs_control_pct}], by_type:[...], by_floor:[...], summary, caveat}`. 개정 적용일 앞뒤 같은 기간의 일평균 매출을 비교하고, 같은 층에서 바뀌지 않은 매장을 비교군으로 쓴다. 인과 효과가 아니다. |
| GET | `/api/reports/relayout-review/{rev}?window_days=90` | 위 결과의 보고서(`text/html`) |

### 상태 검사 및 미러 쓰기

- `GET /healthz`는 파일 이름만 살펴보고 `{ok: true, revisions: [...]}`를 반환한다. DB에 접속하거나 JSON을 파싱하지 않으며 컨테이너의 liveness 검사에 사용한다.
- `GET /readyz`는 읽을 수 있는 작업공간 개정이 한 개 이상이면 200, 없거나 손상되었으면 503을 반환한다. `{ok, workspace_loaded, db: "ok"|"down"|"disabled", mirror_errors, last_mirror_error, last_mirror_error_at, writes_available, pending_mirror_writes}`를 담는다. `mirror_errors`에는 재시도로 복구한 연결 오류도 포함한다. `last_mirror_error`는 예외 유형만 기록하며 시각은 UTC ISO 8601이다. DB가 중지되어도 메모리에 이미 적재한 작업공간은 200을 유지한다.
- `GET /api/meta`에도 같은 미러 상태 필드를 추가한다. `writes_available`이 `false`면 업로드·승인 등 쓰기 작업을 시도하기 전에 경고한다. 로컬 저장에 성공해도 미러 게시가 실패한 요청은 오류를 반환하고, 미러가 복구되면 미게시 파일·삭제를 다시 보낸다. 미러를 설정하지 않은 로컬 환경은 `db: "disabled"`, `writes_available: true`다.

### 1.1 시즌 개편(relayout)

개편 브리프는 사람이 만든 메타데이터다. 시즌, 기준 개정, 대상 층, 목적, 입점·퇴점 브랜드를 담는다. 브리프는 `relayout_brief` 메타데이터로 올린다(1.2절). 백엔드는 브리프마다 개편 프로젝트(진단 → 대안 → 선택 → 도면 대조 → 보고서)를 저장한다.

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/api/relayout/briefs` | `[{brief_id, title, season, year, effective_date, base_rev, floors, objective, status, entries, exits, selected_alt_id, updated_at}]`. `entries`·`exits`는 건수. `status`는 `draft`, `diagnosed`, `planned`, `selected`, `checked` 중 하나다. 브리프 파일에 오류가 있으면 `{brief_id, title, status: "invalid", error}`만 준다. |
| GET | `/api/relayout/{brief_id}` | 프로젝트(아래). 없는 브리프면 404 |
| POST | `/api/relayout/{brief_id}/diagnose` | 본문 없음 → `diagnosis` |
| POST | `/api/relayout/{brief_id}/plan` | 본문 `{objective?: "sales"\|"commission", n_alternatives?: 1~5, max_moves?, downtime_weeks?, movable?: "expiring"\|"all", extra_exits?: [브랜드 ID나 이름], extra_entries?: [브랜드 ID나 이름], as_of?: "YYYY-MM-DD", replace_decision?: false}` → `plan`과 서버 선택 상태. 보존할 결정·대조·공사 순서가 있으면 같은 조건은 `reused: true`, 다른 조건은 설명을 담은 409다. 사용자의 확인 후에만 `replace_decision: true`로 재시도한다. |
| POST | `/api/relayout/{brief_id}/select` | 본문 `{alt_id, decided_by}` → 프로젝트. 사람이 고른 대안을 기록한다. |
| POST | `/api/relayout/{brief_id}/check` | 본문 `{rev}`(CAD팀이 그려 올린 실제 개정) → `check`. 선택한 대안이 없으면 400 |
| POST | `/api/relayout/{brief_id}/phasing` | 본문 `{alt_id?, crews?: 1~6, fitout_weeks?: 1~12, temporary_sales_ratio?: 0~1, use_temporary?: true}` → `phasing`. `alt_id`를 생략하면 선택한 대안, 선택 전이면 기존 비교 지표가 가장 큰 대안을 임시 기준으로 쓴다. `basis`·`basis_note`로 구분하고 프로젝트에 저장한다. |
| GET | `/api/relayout/{brief_id}/report` | 개편 제안서(`text/html`). 진단, 대안 비교, 선택안, 공사 순서, 도면 대조, 가정과 한계를 담는다. |

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

- `diagnosis`, `plan`, `check`, `phasing`은 아직 만들지 않았으면 `null`이다. `report_url`은 항상 있다. 제안서는 요청할 때 그 시점의 프로젝트로 만든다.
- `decision_snapshot`은 선택 시점의 `{alt_id, rev_id, kpi, search_status, plan_params, decided_by, decided_at, decision_source?}`다. `decision_history`는 추가 전용 배열이다. 선택할 때 `{alt_id, decided_by, decided_at, plan_params, kpi_snapshot, decision_source?}`를 추가하고, 조건을 바꿔 교체할 때 이전 결정을 `replaced_at`과 함께 추가한다. 앞선 기록과 KPI는 수정하지 않는다.
- REST 선택의 `decision_source`는 `api`다. 채팅에서 출처를 확인할 수 있으면 `chat`을 쓰고, 확인할 수 없으면 생략한다. 선택한 대안이 제약을 충족하지 않으면 400으로 거부한다.
- 브리프의 선택적 `data_cutoff`와 계획의 `as_of`는 거래 데이터 기준일이다. 계획에서 생략하면 브리프 기준일 또는 적용일 전날이다. 입점 `commission_rate`는 유한한 0~1 비율(예: 20%는 `0.2`)이어야 한다.

**`diagnosis`**

```json
{
  "as_of": "2026-09-30", "lease_cutoff": "2026-10-01",
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

- `season_factors`: `as_of`까지의 카테고리 해당 시즌 일평균 매출을 같은 데이터 기간의 전체 일평균 매출로 나눈 값이다. 1보다 크면 그 시즌에 강하다. 이후 거래는 제외한다.
- `season.last`, `season.previous`: 최근 같은 시즌과 1년 전 같은 시즌이다. 두 해 모두 데이터가 있는 날짜 범위로 맞춘다.
- `floors.yoy_pct`: 1년 전 같은 시즌과 비교한 일평균 매출 증감률이다.
- `candidates`: 대상 층에서 `lease_end < lease_cutoff`이거나 저효율인 매장이다. 계획·진단의 `lease_cutoff`는 개편 적용일이다. 계약 정보가 없으면 만료 여부를 추정하지 않으며 `movable=expiring`에서 기존 브랜드를 이동하지 않는다. 전체 이동은 `movable=all`로 명시해야 한다.
- `entries.feasible_units`: 공실과 퇴점 매장 중 면적·선호 층 조건에 맞는 매장 수다.

**`plan`**

```json
{
  "params": {"objective": "commission", "season": "FW", "max_moves": 6, "downtime_weeks": 2, "movable": "expiring", "floors": ["4F", "5F", "6F"],
             "extra_exits": [], "extra_entries": [], "n_alternatives": 3, "as_of": "2026-09-30", "lease_cutoff": "2026-10-01"},
  "as_of": "2026-09-30", "lease_cutoff": "2026-10-01",
  "season_horizon": {"start": "2026-10-01", "end": "2027-02-28", "days": 151, "annualization_days": 365},
  "current": {"sales_krw": 0, "objective_krw": 0},
  "baseline": {"sales_krw": 0, "objective_krw": 0, "note": "퇴점·입점만 반영한 기준안"},
  "alternatives": [{
    "alt_id": "ALT-1", "rev_id": "H8", "summary": "...", "search_status": "optimal",
    "objective_rounding_krw": 100, "bound_krw": 0, "gap_krw": 0, "all_constraints_satisfied": true,
    "kpi": {"sales_delta_krw": 0, "sales_low_krw": 0, "sales_high_krw": 0, "objective_delta_krw": 0, "move_cost_krw": 0, "net_krw": 0,
            "annual_uplift_krw": 0, "one_off_loss_krw": 0, "season_net_krw": 0, "vs_current_sales_krw": 0, "moves": 3},
    "changes": [{"type": "move", "brand_id": "BR0042", "brand_name": "...", "category_name": "...", "from_unit": "4F-C10",
                 "to_unit": "4F-C05", "from_floor": "4F", "to_floor": "4F", "delta_krw": 0, "explanation": "..."}],
    "constraints": [{"name": "앵커 고정", "ok": true, "detail": "..."}],
    "explanations": ["이웃 매장 인접 친화도 변화에 따른 파급 효과: ..."]
  }],
  "search": {"status": "optimal", "objective_rounding_krw": 100, "net_upper_bound_krw": 0,
             "note": "100원 단위로 반올림한 모델과 제약 안에서 기존 비교 지표(net_krw)의 최적해를 확인했어요. 첫 시즌 순효과의 최적성을 뜻하지는 않아요."},
  "entry_assignment": {"method": "exact_cardinality_then_score", "placed": 4, "requested": 4,
                       "objective_rounding_krw": 1, "scope": "vacant_and_exit_units"},
  "unplaced": [{"brand_id": "BR0470", "brand_name": "...", "reason": "면적·선호 층에 맞는 빈 매장이 없습니다."}],
  "caveats": ["..."]
}
```

- `changes.type`은 `move`(이전), `entry`(입점, `from_unit` 없음), `exit`(퇴점, `to_unit` 없음) 중 하나다. 변경마다 `explanation`(근거)이 있다.
- `explanations`는 변경 하나에 속하지 않는 참고 설명이다. 예: 입점 브랜드 위치 조정, 이웃 매장의 인접 친화도 파급 효과.
- `search_status`는 `optimal`, `optimal_diverse`, `feasible`, `heuristic` 중 하나다. `optimal`은 다양성 벌점·이전 대안 제외 조건이 없는 첫 대안에서 증명한 CP-SAT 해를 실제 채택했을 때만 쓴다. 범위는 **100원 단위로 반올림한 모델과 제약 안에서**다. 후속 대안의 제한된 문제 최적성은 `optimal_diverse`다. 증명하지 못했거나 탐색 배치를 유지했으면 `feasible`, 전체 이전 문제의 정수계획 결과가 없으면 `heuristic`이다. 기준안(ALT-0)은 `heuristic`이다.
- `objective_rounding_krw`는 정수계획의 금액 계수 단위(현재 100)이며 정수계획 결과가 없으면 `null`이다. `bound_krw`는 해당 대안의 다양성 벌점과 반올림 오차 한도를 반영한 기준안 대비 비교 지표 상한, `gap_krw`는 그 상한에서 채택 배치의 벌점 적용 비교 지표를 뺀 값이다. 없으면 두 필드를 생략한다. 반올림 오차 때문에 `optimal`에서도 간격이 0일 필요는 없다. `search`와 기존 `net_upper_bound_krw`는 첫 문제 기준이며 후속 문제의 상한과 구분한다. 계산 방법은 [MCP 도구 명세 5절](mcp-tools.md#5-시즌-개편-계산)에 있다.
- `current`는 지금 배치, `baseline`은 퇴점·입점만 반영하고 기존 브랜드를 옮기지 않은 배치다. 대안의 `kpi`는 `baseline` 대비 변화다. `vs_current_sales_krw`는 지금 배치 대비 기대 매출 변화다.
- `objective_krw`는 시즌 가중치를 곱한 기대 매출이다. `objective=commission`이면 여기에 수수료율도 곱한다.
- `annual_uplift_krw`는 `objective_delta_krw`와 같은 **연간 증가 효과**다. `one_off_loss_krw`는 `move_cost_krw`와 같은 **1회성 공사 손실**이다. 공사 기간(`downtime_weeks`) 동안 이전하는 기존 브랜드의 영업 손실이며 입점 브랜드에는 붙지 않는다.
- `season_net_krw = annual_uplift_krw × season_horizon.days / 365 - one_off_loss_krw`가 **첫 시즌 기준 순효과**다. 기간은 적용일과 시즌 시작일 중 늦은 날부터 시즌 말일까지이며 시즌이 끝났으면 0일이다. `net_krw = objective_delta_krw - move_cost_krw`는 호환성과 기존 순위를 위한 비교 지표로 그대로 유지한다. 이를 연간 순효과라고 표시하지 않는다.
- 모든 금액은 추정치다. 공사비·이사비 등 실제 지출과 투자 회수 기간은 포함하지 않는다. 대안마다 가상 개정 `rev_id`가 저장된다. 화면은 `rev=rev_id`, `compareRev=base_rev`로 대안을 도면에 보여 줄 수 있다.
- 입점 배정은 공실·퇴점 매장 전체에서 입점 수를 먼저 최대화하고 다음에 위치 점수(원 단위)를 최대화한다. 기존 이웃 및 입점 브랜드 사이 인접 충돌도 제한한다. `unplaced`는 함께 배치할 자리를 찾지 못한 브랜드다. `reason`, `requires_incumbent_relocation`, `relocation_unit_codes`로 기존 브랜드를 먼저 옮겨야 하는 제약을 구분한다. 기존 브랜드 이동과 신규 입점을 하나의 문제로 함께 최적화하지는 않는다.
- 인접 충돌 예외는 **실제 개편 전 배치에 이미 있던 매장·브랜드 쌍**만 허용한다. 입점 후 기준안에 생긴 충돌은 예외가 아니다. `all_constraints_satisfied: false`인 대안은 실행 가능한 대안으로 설명하거나 선택할 수 없다.
- POST 계획 응답은 위 `plan` 키와 함께 `selected_alt_id`, `decided_by`, `decided_at`을 항상 반환한다. UI는 이 서버 상태로 선택을 갱신한다. 같은 조건으로 기존 결정을 재사용하면 저장된 계획·선택·대조·공사 순서를 변경하지 않고 `reused: true`를 추가한다. 조건 변경 시 409의 `detail`을 표시하고 확인을 받은 후에만 `replace_decision: true`로 재요청한다. 확인한 교체는 이전 결정을 이력에 남긴 뒤 선택·대조·공사 순서를 초기화한다.
- 입점 후보 조회 도구의 `lease_cutoff`는 같은 배타적 기준(`lease_end < lease_cutoff`)을 쓴다. 생략하면 `as_of`에서 183일 뒤다. `lease_cutoff_note`로 기준을 설명하며 계획과 후보를 맞추려면 계획의 기준일을 명시한다.

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

**`phasing`(공사 순서)**

```json
{"brief_id": "FW2026", "alt_id": "ALT-2", "params": {"crews": 2, "fitout_weeks": 2, "temporary_sales_ratio": 0.6, "use_temporary": true},
 "basis": "selected", "basis_note": "사람이 선택한 ALT-2를 기준으로 계산했어요.", "as_of": "2026-09-30",
 "steps": [{"step": 1, "start_week": 0, "end_week": 2, "action": "move", "action_label": "이전 공사", "unit_code": "6F-E04", "floor": "6F",
            "brand_id": "BR0441", "brand_name": "...", "from_unit": "6F-S02B", "waits_for": [], "closed_weeks": 0, "temporary_unit": null,
            "lost_krw": 0, "note": "기존 매장에서 영업하다가 공사가 끝나면 옮겨요."}],
 "total_weeks": 6, "lost_krw": 0, "naive_lost_krw": 0, "saved_krw": 0, "entry_delay_krw": 0,
 "entries": [{"brand_id": "BR0441", "brand_name": "...", "unit_code": "6F-S02B", "open_week": 4, "earliest_week": 4}],
 "cycles": [{"brands": ["...", "..."], "units": ["4F-C06", "4F-C20"], "breaker": "...", "resolved_by": "temporary", "unit_code": "4F-E02", "weeks": 4}],
 "summary": "...", "caveats": ["..."]}
```

- `action`은 `exit`(퇴점·철거), `move`(이전 공사), `entry`(신규 입점 공사), `temporary`(임시 매장으로 옮김), `close`(임시 휴점) 중 하나다. `waits_for`는 먼저 끝나야 하는 단계 번호다.
- `basis=selected`는 선택한 대안 또는 요청에서 명시한 `alt_id`를 사용했다는 뜻이며 `basis_note`가 둘을 구분한다. 둘 다 없으면 `basis=best_net_fallback`이고, 기존 비교 지표가 가장 큰 대안을 썼다는 설명을 반환한다. 임시 계산으로 프로젝트 선택을 바꾸지는 않는다.
- 공사는 비어 있는 매장에서만 시작한다. 옮기는 브랜드는 새 매장 공사가 끝날 때까지 기존 매장에서 영업한다고 본다. 서로의 자리로 옮기는 순환이 있으면 주 매출이 가장 적은 브랜드(`breaker`)가 먼저 자리를 비운다. 이 브랜드는 임시 매장으로 옮기거나 새 매장이 열릴 때까지 휴점한다. 임시 매장은 대상 층의 공실과 퇴점 매장 중 그 기간에 공사하지 않는 곳이다.
- `lost_krw`는 휴점·임시 매장 영업으로 잃는 기대 매출 추정이다. `naive_lost_krw`는 대안 계산의 가정(옮기는 브랜드마다 공사 기간만큼 휴점)으로 계산한 값이다. 주 매출은 기준 개정의 연 매출(실적이 없으면 추정치)을 52로 나눈 값이다.
- `entry_delay_krw`는 공사팀이 모자라 신규 입점이 `earliest_week`(앞 공사를 기다린 뒤 가장 빨리 열 수 있는 주차)보다 늦게 열리는 기간의 기대 매출이다.
- 동시에 진행하는 공사는 `crews`개까지다. 주 단위이며 0주차가 공사 시작이다. 순서는 세 가지 우선순위(긴 대기 사슬 먼저, 입점 매출 먼저, 남은 공사당 입점 매출 먼저)로 짜 보고 `lost_krw + entry_delay_krw`가 가장 작은 안을 고른다. 같으면 총 기간이 짧은 안을 고른다.
- 같은 조건의 계획 재호출은 `phasing`을 보존한다. 다른 조건은 409 확인 절차를 거친 교체에서만 `null`로 만든다. 다른 대안을 선택하면 그 대안의 공사 순서가 아닐 때 `null`이 된다.
- 도면 업로드 자동 대조가 적용되지 않으면 `relayout_after_upload`는 항상 `{skipped: true, reason: "한국어 설명"}`을 반환한다. 부모 개정 없음, 기준 개정·적용일 불일치, 맞는 브리프에 선택한 대안 없음 등을 구분하며 실패로 취급하지 않는다.

### 1.2 도면·메타데이터 업로드

사람이 만든 도면(DXF)과 메타데이터를 올리면 백엔드가 검증하고 처리한다. 처리 결과는 작업공간과 미러에 저장한다.

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/api/uploads/rules` | 개정 ID 검증 규칙 `{rev_id_pattern, reserved_patterns, max_length}`. `rev_id_pattern`은 `^[A-Z0-9][A-Z0-9_-]{0,15}$`, `reserved_patterns`는 `["^H[0-9]+$"]`, `max_length`는 16이다. |
| POST | `/api/uploads/drawings` | multipart: `files`(DXF 여러 개 또는 ZIP 1개), `rev_id`, `parent_rev?`(기본: 실적이 있는 가장 최근 실제 개정), `effective_date`(YYYY-MM-DD), `units?`(`m`·`mm`, 기본: 프로필 값), `profile?`(도면 규약 프로필 ID, 기본 `default`), `run_generators?`(`true`·`false`, 기본 `true`) → `202 {job_id, status}`. 형식 오류는 400, 같은 ID의 기존 개정(업로드로 만든 개정 제외)은 409 |
| GET | `/api/uploads/jobs` | 최근 작업 목록(최신순, 최대 20개) `[{job_id, kind, status, rev_id, created_at, updated_at, revision_available, review_reason, stage_errors, superseded_by}]` |
| GET | `/api/uploads/jobs/{job_id}` | 작업 상태와 결과(아래). 없으면 404 |
| POST | `/api/uploads/jobs/{job_id}/layer-mapping` | 본문 `{mappings: [{layer, semantic, floor?}]}` → 매핑을 저장하고 다시 처리한다. 응답은 작업 상태다. |
| GET | `/api/uploads/profiles` | 도면 규약 프로필 목록 `[{profile_id, title, description, units}]`. `default`가 항상 있다. |
| POST | `/api/uploads/metadata` | multipart: `kind`(`brands`, `tenancies`, `leases`, `pos_terminals`, `relayout_brief`, `drawing_profile`, `stores`, `store_kpis`), `file`(CSV, XLSX, YAML, JSON) → `{kind, status: "accepted"\|"rejected", rows, columns, errors, warnings, backup, id?}`. `id`는 브리프·프로필의 ID다. 내용 거부는 200으로 응답하고 기존 데이터는 바꾸지 않는다. 도면 작업이 대기·처리 중이면 409로 응답한다. |
| GET | `/api/uploads/metadata` | 종류별 현재 상태 `[{kind, present, rows, updated_at, required_columns, items, source, source_basis, filename, uploaded_at, sha256, stored_sha256, record_matches_content}]`. `items`는 브리프·프로필의 ID 목록(표 종류는 빈 배열)이고, `item_sources`는 문서별 출처다. |

**도면 파일 규칙**

- 파일 이름에 층 코드가 있어야 한다. 예: `R3_4F.dxf`, `6F.dxf`, `B1.dxf`, `4층.dxf`. 같은 층 파일이 둘이면 400이다.
- 파일 하나는 30MB, 전체는 100MB까지 받는다. ZIP 안의 DXF가 아닌 파일은 무시한다.
- `rev_id`는 영문 대문자·숫자·`-`·`_`로 1~16자이며 영문 대문자나 숫자로 시작한다. `H` 뒤에 숫자만 붙은 ID는 가상 개정에 예약되어 사용할 수 없다.
- 작업은 한 번에 하나씩 처리한다. 앞 작업이 있으면 `queued`로 기다린다.

**메타데이터 종류**

| `kind` | 형식 | 필수 열·키 | 저장 |
|---|---|---|---|
| `brands` | CSV·XLSX | `brand_id`, `brand_name`, `category` | 표 전체 교체 |
| `tenancies` | CSV·XLSX | `brand_id`, `unit_code`, `rev_id`, `valid_from`, `valid_to` | 표 전체 교체 |
| `leases` | CSV·XLSX | `lease_id`, `brand_id`, `unit_code`, `lease_start`, `lease_end` (선택: `contract_area_m2`, `commission_rate`) | 표 전체 교체 |
| `pos_terminals` | CSV·XLSX | `pos_id`, `brand_id`, `unit_code`, `valid_from`, `valid_to` | POS 단말을 브랜드·매장·유효 기간에 연결하는 표 전체 교체 |
| `relayout_brief` | YAML·JSON | `brief_id`, `title`, `season`(`SS`·`FW`), `year`, `effective_date`, `base_rev`, `floors` | 브리프 하나 추가·교체 |
| `drawing_profile` | YAML·JSON | `profile_id`, `title` | 프로필 하나 추가·교체 |
| `stores` | CSV·XLSX | `store_id`, `store_name`, `region`, `format`, `gross_area_m2` | 표 전체 교체(비교 대상 점포 목록) |
| `store_kpis` | CSV·XLSX | `store_id`, `period`(YYYY-MM), `floor`, `category`, `area_m2`, `sales_krw` | 표 전체 교체(점포별 월·층·카테고리 실적) |

- 표를 교체하기 전에 이전 파일을 `backup`에 적힌 경로로 보관한다.
- 오류(`errors`)가 하나라도 있으면 거부한다. 경고(`warnings`)만 있으면 받아들인다.
- `pos_terminals`는 단말 매핑이다. 거래·매출 파일을 올리는 기능은 없다.
- `source`는 기본 합성 시연 데이터면 `synthetic_demo`, 성공적으로 올린 자료면 `uploaded`다. 여러 브리프·프로필의 출처가 섞이면 `mixed`다. 파일이 없으면 `present: false`다.
- `source_basis: default_assumption`은 업로드 기록이 없어 기본 데이터로 분류했다는 뜻이며 파일을 직접 교체했는지는 확인하지 못한다. `upload_index`는 업로드 기록, `builtin`은 내장 프로필, `not_present`는 파일 없음이다. 직접 교체한 파일은 출처를 별도로 확인한다.
- 브리프·프로필에 올린 항목이 있으면 최상위 파일 정보는 가장 최근 업로드의 정보다. 항목별 정보는 `item_sources`에서 확인한다.
- `filename`·`uploaded_at`·`sha256`은 성공한 업로드의 원본 파일 정보다. `stored_sha256`은 정규화해 저장한 파일의 해시다. `record_matches_content: false`이면 현재 파일이 기록과 달라 출처를 다시 확인해야 한다.
- 도면 작업이 하나라도 `queued` 또는 `running`이면 모든 메타데이터 종류의 교체를 차단한다. 작업이 끝난 뒤 다시 올린다.

**작업 상태(`GET /api/uploads/jobs/{job_id}`)**

```json
{
  "job_id": "J20261023-0001", "kind": "drawing", "status": "needs_review", "rev_id": "R3", "parent_rev": "R2",
  "effective_date": "2026-10-01", "profile": "default", "created_at": "...", "updated_at": "...",
  "files": [{"name": "R3_4F.dxf", "floor": "4F", "bytes": 381220,
             "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],
  "source_files": [{"name": "R3_4F.dxf", "bytes": 381220,
                    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],
  "revision_available": true, "review_reason": "layer_review_queue", "stage_errors": [],
  "superseded_by": null, "metadata_snapshot": {"brands": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
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

- `status`는 `queued`, `running`, `needs_review`, `done`, `partial`, `failed` 중 하나다. 모델을 저장하고 레이어 검토 큐에 항목이 있으면 `needs_review`다. 모델을 저장한 뒤 후속 단계가 하나라도 실패하면 검토 큐 유무와 관계없이 `partial`이며 `stage_errors: [{step, message}]`에 실패한 단계를 기록한다.
- 산출물 생성 결과가 `qa_failed`이면 생성 단계의 실패로 기록한다. 생성된 파일과 검증 이슈는 산출물 조회 API에서 확인할 수 있다.
- `revision_available`은 이 작업이 저장한 모델이 현재 개정 모델인지 나타낸다. `review_reason`은 레이어 검토가 필요할 때 `layer_review_queue`, 아니면 `null`이다. 두 필드와 작업 상태는 개정 승인 여부가 아니다. 개정 승인·반려 기능은 구현하지 않았다.
- 각 `files.sha256`은 실제로 처리한 DXF의 SHA-256이다(ZIP은 압축을 푼 각 DXF 기준). `source_files`는 받은 원본 파일마다 이름·크기·SHA-256을 기록하므로 ZIP 파일 자체도 식별할 수 있다. `metadata_snapshot`의 값은 작업 시작 시 존재한 업무 표의 저장 파일 SHA-256이다.
- 이 필드가 도입되기 전의 작업은 저장된 단계 결과와 모델 작업 ID로 상태를 보완한다. 당시 기록하지 않은 `metadata_snapshot`은 빈 객체다.
- 업로드 개정을 같은 ID로 다시 올려 새 모델을 저장하면 이전 작업의 `superseded_by`에 새 `job_id`를 기록하고 이전 작업의 `revision_available`을 `false`로 바꾼다. 새 모델을 저장하기 전에 실패한 재업로드는 이전 모델을 대체하지 않는다.
- `steps.name`은 `save`(파일 저장), `parse`(도면 읽기), `merge`(층 합치기), `diff`(변경 비교), `qa`(계약 정보 대조), `impact`(영향 분석), `generate`(산출물 재생성), `relayout_check`(개편안 대조) 순서다. `steps.status`는 `pending`, `running`, `done`, `skipped`, `failed` 중 하나다.
- 올리지 않은 층은 부모 개정에서 그대로 가져온다(`floors_inherited`).
- `qa`는 도면을 계약 정보(`leases`, 적용일에 유효한 계약만)와 대조한 결과다. `code`는 `unit_missing_in_drawing`(계약 매장이 도면에 없음), `unit_without_lease`(도면에 브랜드가 있지만 계약 없음), `brand_mismatch`(도면 브랜드와 계약 브랜드가 다름), `area_mismatch`(계약 면적과 도면 면적이 3% 넘게 다름) 중 하나다.
- `relayout_check`는 이 개정의 부모가 선택된 개편안의 기준 개정이고 적용일이 브리프 적용일과 같으면 자동으로 채워진다. 형식은 1.1절 `check`의 요약이다. 조건에 맞지 않으면 `{skipped: true, reason}`을 기록하며 이 단계는 실패가 아니다.
- `semantic`은 [CAD 파이프라인](cad-pipeline.md)의 레이어 의미 16개 중 하나다.

## 2. 채팅(AG-UI 호환 SSE)

**요청:** `POST /api/chat`

```json
{"thread_id": "optional", "message": "사용자 질문", "ui_state": {"rev": "R2", "floor": "3F", "selection": ["3F-N05"], "overlays": ["heatmap"]}}
```

`ui_state.overlays`는 지금 화면에 보이는 표시 이름(`heatmap`, `highlights`, `edges`, `flow`, `route`, `journey`, `compare`)이다. 백엔드는 이 목록으로 이전 답변의 표시만 골라 지운다(3절). 에이전트에게는 `rev`, `floor`, `selection`, `compareRev`, `overlays`만 전달한다.

**응답:** `text/event-stream`이다. 이벤트는 `data: {"type": ..., ...}` 다음에 빈 줄을 둔다. 이벤트가 없으면 15초마다 SSE 주석 `: keepalive`와 빈 줄을 보낸다. 주석은 화면 이벤트가 아니다. 서버의 전체 턴 제한(`CHAT_TURN_DEADLINE_S`, 기본 230초)에 걸리면 한국어 안내가 담긴 `RUN_ERROR`로 종료하며, 연결이 끊기면 실행 취소를 요청한다.

| type | 필드 | 의미 |
|---|---|---|
| `RUN_STARTED` | `thread_id`, `run_id`, `agent?`, `agent_version?` | 실행 시작. `agent`는 오케스트레이터 에이전트 이름, `agent_version`은 `FOUNDRY_AGENT_VERSIONS`로 고정한 버전이다(고정하지 않으면 없다). |
| `TEXT_MESSAGE_START` | `message_id` | 어시스턴트 메시지 시작. 한 실행에 메시지는 하나다. |
| `TEXT_MESSAGE_CONTENT` | `message_id`, `delta` | 답변 텍스트 조각. 모델이 최종 답변(`phase: final_answer`)이라고 표시한 메시지는 생성하는 대로 스트리밍한다. 표시가 없는 메시지는 다음 출력이 정해질 때까지 모았다가 보낸다. 뒤에 도구 호출이 오면 진행 노트, 응답이 끝나면 답변이다. 진행 노트는 답변으로 보내지 않는다. |
| `TEXT_MESSAGE_END` | `message_id` | 메시지 종료 |
| `THINKING_TEXT_MESSAGE_START` | `message_id`, `parent_tool_call_id?`, `agent?` | 진행 노트 시작. 에이전트가 도구를 부르기 직전에 쓴 한 문장(무엇을 어떤 방법으로 확인할지, 또는 방법을 바꾸는 이유)이다. 답변이 아니다. 전문 에이전트 안의 노트는 `parent_tool_call_id`와 `agent`를 붙인다. |
| `THINKING_TEXT_MESSAGE_CONTENT` | `message_id`, `delta` | 진행 노트 텍스트 |
| `THINKING_TEXT_MESSAGE_END` | `message_id` | 진행 노트 종료 |
| `TOOL_CALL_START` | `tool_call_id`, `tool_name`, `args`, `label`, `method?`, `parent_tool_call_id?`, `agent?` | 도구 호출 시작. `label`은 사용자에게 보여 줄 한국어 단계 이름이다(예: "4층 공간 생산성 계산"). `method`는 그 단계가 답을 계산하는 방법을 한 문장으로 쓴 것이다(예: "결제 실적을 연 매출로 환산해 ㎡당 매출을 구하고 층 하위 10%를 저효율로 봐요"). |
| `TOOL_CALL_END` | `tool_call_id`, `summary`, `label?`, `method?`, `result?`, `ok?` | 도구 호출 종료. `label`이 있으면 시작 때의 설명을 이 값으로 바꾼다. `result`는 도구 결과에서 읽은 한 줄 요약이다(예: "매장 50곳 중 저효율 5곳 · 가장 낮은 곳 Ravandre Nuit(4F-E05)"). 할 말이 없으면 `null`이다. 도구가 인자 오류를 돌려주면 `ok`는 `false`, `result`는 "조건을 고쳐 다시 시도해요"이다. 기존 결정 때문에 거절되면(`code: decision_exists`) "기존 결정이 있어 사용자 확인이 필요해요", 도구 내부 오류(`code: internal_error`)면 "도구 내부 오류로 이 단계를 하지 못했어요"다. 응답 스트림이 끊겨 결과를 받지 못한 단계도 `ok: false`로 끝낸다. `summary`는 개발자용 원문이다. |
| `STATE_DELTA` | `delta` | UI 상태에 적용할 JSON Patch(RFC 6902) 배열 |
| `RUN_FINISHED` | `thread_id`, `run_id`, `outcome`, `incomplete?` | 실행 종료. `outcome`은 `answered`(에이전트가 답변을 씀)이거나, 답변 없이 끝난 이유인 `no_answer`, `tool_failed`(실패한 단계가 있음), `rounds_exhausted`(도구 호출 라운드 한도), `time_exhausted`(턴 시간 예산)다. 이유가 있으면 `incomplete: true`이고, 답변은 완료한 단계와 실패한 단계를 나열한 "(미완료)" 안내문이다. 진행 노트를 답변으로 바꾸지 않는다. |
| `RUN_ERROR` | `message`, `code?`, `error_type?` | 오류. `code`는 `rate_limit`, `auth`, `transient`, `rejected`, `other` 중 하나이고, 서버가 취소를 요청하면(연결 끊김, 턴 제한) `cancelled`와 "요청이 취소됐어요."다. `error_type`은 예외 클래스 이름이다. |

에이전트 응답 스트림이 이번 실행에서 도구 결과를 받은 뒤 일시적 오류(연결, 시간 초과, 5xx)로 끊기면, 백엔드는 받은 결과를 담아 응답을 한 번 다시 요청한다. 함수 도구(UI 함수, 전문 에이전트 상담)는 다시 실행하지 않고, 끝난 서버 도구는 다시 부르지 말라고 에이전트에게 알린다. 요청 한도(429)와 권한·요청 오류는 다시 요청하지 않는다.

**화면 표시 원칙:** 채팅은 대화처럼 보여야 한다. 실행 중에는 "분석 과정"을 펼쳐 진행 노트와 단계(이름, 계산 방법, 결과)를 도착한 순서대로 보여 준다. 전문 에이전트의 단계와 노트는 그 상담 단계 아래에 들여 쓴다. 답변이 나오기 시작하면 "분석 과정 보기 · 4단계 · 23초" 한 줄로 접는다(단계 수에 진행 노트는 넣지 않는다). 도구 이름, 인자, `summary` 원문은 "기술 정보"를 켰을 때만 보인다. `method`와 `result`는 코드가 정한 문장과 도구 결과에서 만든다. 진행 노트만 에이전트가 쓴다.

## 3. UI 상태 모델

에이전트의 UI 함수 도구 호출은 백엔드가 `STATE_DELTA`로 바꿔 보낸다. UI 함수 도구는 `ui_set_floor`, `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_focus`, `ui_compare`, `ui_show_panel`, `ui_show_journey`, `ui_show_route`, `ui_show_flow`, `ui_clear`이다.

```json
{
  "rev": "R2",
  "compareRev": null,
  "floor": "3F",
  "layers": {"fixtures": false, "walkways": true, "labels": true},
  "heatmap": {"metric": "sales_per_m2", "label": "평효율(원/㎡)", "rev": "R2", "estimated": false, "values": {"3F-N05": 0.82}},
  "highlights": [{"unit_code": "3F-N05", "color": "#e53935", "reason": "저효율 하위 10%"}],
  "edges": [{"from": "3F-N05", "to": "2F-C03", "weight": 2.4, "label": "lift 2.4"}],
  "focus": {"unit_codes": ["3F-N05"]},
  "panel": {"type": "changes", "payload": {}},
  "journey": null,
  "route": null,
  "flow": null
}
```

**표시 겹침 규칙:** 동선(`journey`), 경로(`route`), 집계(`flow`)는 단독으로 보이도록 이전 답변의 다른 표시(`heatmap`, `highlights`, `edges`, `compareRev`, 다른 경로 표시)를 비운다. 반대로 `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_compare`는 이전 답변의 동선·경로·집계를 비운다. 같은 답변 안에서 먼저 그린 표시는 비우지 않는다(예: 후보 매장 강조 뒤의 팝업 후보 표시). 백엔드는 요청의 `ui_state.overlays`(또는 값이 있는 상태 필드)로 이전 답변의 표시를 알고, 보이는 것만 비운다. 클라이언트가 이 정보를 보내지 않으면 겹칠 수 있는 표시를 모두 비운다. 표시하는 개정(`/rev`)이 바뀌면 다른 개정에 그린 표시는 같은 답변의 것이라도 비운다.

클라이언트는 `STATE_DELTA`에 포함된 지우기 작업만 적용한다. 같은 답변의 패치 사이에서 표시를 추가로 지우지 않는다.

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
- 동선과 다른 분석 표시는 함께 두지 않는다(위 표시 겹침 규칙). `ui_show_journey`는 이전 답변의 `heatmap`, `highlights`, `edges`, `compareRev`, `route`, `flow`를 비운다. 이후 답변의 `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_compare`는 `journey`를 `null`로 바꾼다. 같은 답변 안에서 그린 표시는 유지한다. `segments`가 비어 있으면 결과의 `shown`은 매장 사이 경로를 그리지 못했다고 말한다.
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
- 경로도 동선처럼 단독으로 표시한다. `ui_show_route`는 이전 답변의 `heatmap`, `highlights`, `edges`, `compareRev`, `journey`, `flow`를 비우고, 이후 답변의 `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_compare`는 `route`를 `null`로 바꾼다. 같은 답변 안에서 그린 표시는 유지한다. 에이전트에게 돌려주는 `shown`은 실제로 그린 것만 말한다. 그릴 `segments`가 없으면 선을 그리지 않았다고, `straight: true`이면 직선(추정)으로 표시했다고, `walk_m`이 `null`이면 걷는 거리를 모른다고 쓴다.

**`flow`(통로 위 집계: 추정 유동, 팝업 후보, 통로 폭·피난 점검)**

```json
{
  "mode": "traffic",
  "title": "40대 여성 VIP 고객의 추정 유동(주말 10~14시)",
  "rev": "R2",
  "query": {"segment": {"age_band": "40s", "gender": "F", "tier": "VIP"}, "day_type": "weekend", "hours": [10, 14], "floor": null},
  "floors": {
    "3F": {"segments": [{"points": [[12.0, 15.0], [40.0, 15.0]], "weight": 0.82, "value": 1234, "color": null, "label": "3F-C05·3F-C06 앞 통로"}],
           "max_value": 1500, "total": 23456}
  },
  "markers": [{"floor": "3F", "x": 60.0, "y": 31.5, "rank": 1, "kind": "spot", "label": "1", "title": "후보 1 · 3F 에스컬레이터 A 앞 통로", "unit_code": null}],
  "legend": {"low": "적음", "high": "많음", "unit": "명(추정)", "note": "결제 순서를 통로망 최단 경로로 이어 모은 값"},
  "summary": {"customer_days": 1234, "trips": 4567, "floors": ["3F", "4F"]},
  "caveat": "결제 순서를 통로망 최단 경로로 이은 추정이에요. 실제로 걸은 길이 아니에요."
}
```

- `mode`는 `traffic`, `popup`, `egress` 중 하나다.
  - `traffic`: `segments`는 통로 중심선 구간이고 `value`는 기간 동안 그 구간을 지난 추정 이동 수, `weight`는 층 최댓값으로 나눈 0~1 값이다. 층의 `total`은 그 층을 걸은 추정 이동 수다(층을 오가는 이동은 두 층에 센다).
  - `popup`: `traffic`과 같은 유동을 바탕에 그리고, `markers`에 팝업 후보 위치를 순위대로 둔다(`kind: spot`은 통로 위 공간, `kind: unit`은 공실·팝업 매장). 후보마다 하루 추정 통행 `per_day`와 추천 이유 `reasons`(문장 목록)가 있다.
  - `egress`: `segments`는 기준보다 좁은 통로 구간이다. `value`는 폭(m), `color`는 경고 색, `weight`는 `0.5 + 0.5 × (1 - 폭/기준 폭)`이다. 층의 `max_value`는 기준 폭, `total`은 좁은 구간 수다. `markers`는 계단·출입구까지 걷는 거리가 기준을 넘는 매장이다(`kind: warning`).
- `summary`에는 `customer_days`, `trips`, `floors`, `period`, `notes`가 있다. `traffic`·`popup`은 많이 지난 통로 `top_corridors`(층, 이름, 기간 합계, 하루 평균, 층 이동 중 비율)를, `traffic`은 앞을 많이 지나는 매장 `top_units`를 함께 준다. `egress`는 `customer_days`·`trips`가 `null`이고 `narrow_stretches`, `units_over_limit`, `unreachable_units`, `text`가 있다.
- 유동은 거래가 쌓인 실제 개정에서만 계산한다. 화면의 개정이 가상 개정이나 아직 실적이 없는 개정이면 그 기준 실제 개정으로 계산하고 `rev`에 그 개정을 넣는다. 이때 `ui_show_flow`는 `/rev`도 함께 바꾼다.
- `segments.color`가 `null`이면 화면이 `weight`로 색과 굵기를 정한다. 값이 있으면 그 색을 쓴다.
- 화면은 현재 층의 `segments`와 `markers`만 그린다. 다른 층에 데이터가 있으면 층 탭에 표시한다. 범례와 `caveat`를 함께 보여 준다.
- 집계 표시도 단독으로 표시한다. `ui_show_flow`는 이전 답변의 `heatmap`, `highlights`, `edges`, `compareRev`, `journey`, `route`를 비운다. 이후 답변의 `ui_show_heatmap`, `ui_highlight_units`, `ui_draw_edges`, `ui_compare`는 `flow`도 `null`로 바꾼다. 같은 답변 안에서 그린 표시는 유지한다.

| 필드 | 규칙 |
|---|---|
| `heatmap`, `heatmap.values` | `values`는 0~1로 정규화한 값이다. `heatmap`이나 `heatmap.values`가 `null`이면 heatmap을 끄고 카테고리 색으로 칠한다. |
| `heatmap.rev`, `heatmap.estimated` | `rev`는 값을 계산한 개정이다. `ui_show_heatmap`은 `/rev`, `/floor`, `/heatmap`을 한 `STATE_DELTA`로 함께 바꾸므로 색은 항상 화면의 개정 기준이다. `estimated`가 `true`이면 실적이 없는 개정(다음 시즌 도면, 가상 개정, 개편 대안)이라 값이 모델 추정치이고, `label` 끝에 "(모델 추정치)"가 붙는다. 범례에 추정치임을 표시한다. |
| `heatmap.legend` | 선택 필드 `{low, high, note, binary}`. 색의 의미가 "값이 클수록 진함"이 아닐 때 범례 문구를 지정한다. `binary`가 `true`이면 범례에 두 색만 표시한다. 예: 저효율 플래그, 에스컬레이터 접근성(가까울수록 진함). |
| `edges` | 다른 층 매장과 이으면 화면 가장자리의 층 배지로 연결해 표시한다. |
| `focus` | 다른 층 매장에 초점을 맞출 때는 `/floor` 변경을 같은 `STATE_DELTA`에 함께 보낸다. |
| `panel.type` | `changes`, `artifact`, `layout`, `table`, `journey`, `uploads`, `none` 중 하나다. `journey`는 `journey` 상태의 동선 목록을 연다. `uploads`는 개정 검토 탭의 업로드 영역을 연다. |
| `panel.payload` | `changes`: `{rev_from, rev_to}`, `artifact`: `{generator}`, `layout`: `{rev, floors, alternatives}` 또는 `{brief_id}`(시즌 개편 프로젝트를 연다), `uploads`: `{job_id?}`(업로드 작업을 연다), `table`: `{title, columns, rows}`. 백엔드는 `table`의 객체 행과 열 단위 데이터를 값 배열 행으로 바꾼다. 행이 없으면 패널을 열지 않고 에이전트에게 오류를 돌려준다. |

채팅 응답 중 `changes`, `artifact`, `layout`, `uploads` 결과가 도착해도 현재 탭을 바꾸지 않는다. 채팅 위의 **개정 검토 보기** 또는 **레이아웃 대안 보기**, 해당 탭의 **새 결과** 표시로 사용자가 직접 연다. 응답 완료와 상태 갱신이 한 번에 도착해도 이 규칙을 유지한다. 채팅 밖에서 명시적으로 여는 패널 요청은 기존대로 해당 탭을 선택한다.

배치 대안 화면은 예상 연매출 변화와 이동 수를 먼저 비교하고, 이동 목록·검사한 제약·계산 근거는 펼쳐 본다. `search_status`가 있는 대안만 최적성 상태를 표시한다. 제약 검사 목록이 비었으면 **제약 검사 미확인**으로 표시한다. 대안이 없는 결과는 현재 조건에서 개선안을 찾지 못한 것이며, 모든 조건에서 현재 배치가 최적이라는 뜻은 아니다.

**표기 규칙**

- SSE는 `data: {...}` 다음에 빈 줄을 두는 표준 형식을 쓴다.
- 필드 이름은 snake_case를 기준으로 한다. 클라이언트는 AG-UI의 camelCase 이름도 받아들이고, 모르는 이벤트 유형은 무시한다.
- `STATE_DELTA`는 전부 적용하거나 전부 버린다. 한 연산이라도 실패하면 화면에 갱신 실패를 표시한다.
- `percentile`은 0~100 척도다.
- 전문 에이전트 안에서 일어난 도구 호출은 `TOOL_CALL_START`에 `parent_tool_call_id`와 `agent`를 붙여 보낸다.
