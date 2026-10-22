# MCP 도구 계약

Atlas 서비스의 `/mcp` 엔드포인트(Streamable HTTP, stateless JSON 응답)가 제공하는 도메인 도구다. 모든 도구는 결정론적 코드로 계산하고 JSON으로 응답한다. 추정치에는 `estimate: true`와 `caveat`(한국어)를 포함한다.

## 1. 원칙

- 산출물 승인·반려 도구는 제공하지 않는다. 승인은 사람이 Atlas 화면이나 API로만 한다.
- 응답은 기본 50행으로 제한하고 `truncated` 플래그를 둔다.
- 인증은 요청 헤더 `x-api-key`로 한다. 값은 Key Vault와 Foundry 프로젝트 연결에만 둔다.
- 인자 오류나 데이터 없음은 MCP 오류(`isError`) 대신 `{ok: false, tool, error, hint}` 결과로 반환한다. Foundry Agent Service는 MCP 도구 오류를 응답 전체의 실패(HTTP 400)로 처리하기 때문이다. 모델은 이 결과를 읽고 인자를 고쳐 다시 호출한다.

## 2. 도구 목록

| 그룹 | 도구 | 주요 인자 | 반환 |
|---|---|---|---|
| spatial | `spatial_list_revisions` | — | 개정 목록(종류, 적용일, 부모) |
| spatial | `spatial_floor_summary` | `rev`, `floor` | 매장 수, 면적, 카테고리 구성, 공실 |
| spatial | `spatial_find_units` | `rev`, `floor?`, `category?`, `brand_query?`, `vacant?`, `limit?`, `sort_by?` | 매장 목록. `sort_by`는 `walk_to_escalator_m`·`walk_to_entrance_m`(가까운 순), `area_m2`(큰 순) |
| spatial | `spatial_unit_details` | `rev`, `unit_code` | 속성, 인접 매장, 통로 frontage, 에스컬레이터·입구 거리, 집기 요약 |
| spatial | `spatial_nearby_units` | `rev`, `unit_code`, `radius_m?` | 반경 안의 같은 층 매장(거리순). PostGIS 사용 가능 시 `ST_DWithin` |
| spatial | `spatial_query` | `rev`, `filters?`, `group_by?`, `metrics?`, `sort_by?`, `fields?`, `limit?`, `start?`, `end?` | 매장 단위 범용 질의. 필드 조건(`eq`·`in`·`gte`·`lt`·`contains` 등), 관계 조건(`adjacent_to`: 벽을 맞댄 매장, `near`: 걸어서 `within_m` 이내), 묶음 집계(`count`, `sum:`, `mean:`, `median:`, `min:`, `max:`, `rate:분자/분모`), 정렬. 필드는 매장 속성, 보행 특징, 공간 어휘(4절), 실제 개정의 연환산 실적, 가상 개정의 기대 매출(추정) |
| spatial | `spatial_route` | `rev`, `origin`, `destination?`, `destination_category?`, `destination_floor?`, `via?` | 통로망 최단 경로의 걷는 거리(`walk_m`, 층별 `legs`), 수직 이동(`transitions`: 수단, 층 수, 높이), 다른 후보. 장소는 매장 코드, 브랜드명, 출입구(`ENT-1`, 정문), 층을 붙인 수직동선·편의시설(`1F:ESC-A`, `8F:화장실`) |
| spatial | `spatial_describe_floor` | `rev`, `floor` | 층의 지도 카드: 크기, 수직동선·출입구·편의시설의 방위, 구역과 방위, 앵커, 에스컬레이터 정면 매장, 메인 동선 매장, 공실, 한국어 요약 |
| revision | `revision_diff` | `rev_from`, `rev_to` | change set 요약(유형별 건수, 항목별 한국어 요약) |
| revision | `revision_process` | `rev`, `variant?` | 파싱 → diff → 영향 분석 → 재생성(초안) 결과 |
| revision | `revision_create_hypothetical` | `base_rev`, `moves:[{brand_id, to_unit}]` | 가상 개정 ID와 change set |
| registry | `registry_list_generators` | — | 생성기 스펙 목록 |
| registry | `registry_impact` | `rev_from`, `rev_to` | 재생성 계획(생성기, 위험도, 승인 방식, 사유) |
| registry | `registry_run` | `generator`, `rev`, `floors?` | 산출물 기록(상태, 파일, 요약, 이슈) |
| registry | `registry_validate` | `rev`, `generator` | 검증 이슈 |
| render | `render_adhoc_table` | `rev`, `entity_kind`, `fields`, `floor?`, `group_by?`, `title` | 즉석 표 산출물(초안)과 레지스트리 등록안 |
| analytics | `analytics_productivity` | `rev`, `floor?`, `level?`, `start?`, `end?`, `top?` | 연환산 ㎡당 매출, 백분위, 저효율 플래그, 저효율 원인 |
| analytics | `analytics_drivers` | `rev`, `floor?` | 회귀 계수, 설명력, 저효율 매장별 주요 요인(추정치) |
| analytics | `analytics_cross_shopping` | `level?`, `focal?`, `segment?`, `start?`, `end?`, `top?`, `basket?`, `min_customers?` | lift, Jaccard, confidence |
| analytics | `analytics_floor_transitions` | `segment?`, `start?`, `end?` | 층간 전이 행렬, 분수·샤워효과 지표 |
| analytics | `analytics_adjacency_effect` | `rev`, `neighbor_category`, `neighborhood?`(`near`·`wall`), `radius_m?` | 단순 추정 vs 노출 통제 추정, 공통 원인 의심 여부 |
| analytics | `analytics_before_after` | `change_date`, `unit_codes`, `window_days?`, `key?` | 전후 비교(인과 아님) |
| analytics | `analytics_time_patterns` | `by`, `floor?`, `category?`, `start?`, `end?`, `limit?` | 시간대·요일·카테고리 패턴 |
| layout | `layout_generate_alternatives` | `rev`, `floors?`, `keep_anchors?`, `area_tolerance?`, `max_moves?`, `n_alternatives?`, `movable_categories?`, `allow_cross_floor?`, `save?` | 대안별 이동, KPI 변화 범위, 제약 충족표, 설명, 가상 개정 ID |
| layout | `whatif_score_moves` | `rev`, `moves` | 이동안의 예상 영향(추정치) |
| analytics | `analytics_customer_journey` | `customer_id?`, `date?`, `segment?`, `rank?`, `min_stops?`, `start?`, `end?`, `rev?` | 가명 고객 한 명의 하루 결제 순서(시각, 층, 매장, 금액, 경과 시간)와 다음 후보. `rev`를 주면 그 개정이 쓰인 기간에서 고른다. 실제 이동 경로가 아님 |

금액은 합성 표본 거래에 표본 가중치를 곱한 전체 규모 값이다. 실적 지표는 개정 적용 기간의 합계를 연 단위로 환산한다. 실제 개정의 분석 기간은 그 개정의 적용일부터 다음 실제 개정 적용일 전날까지다.

## 3. 에이전트별 허용 도구

| 에이전트 | 허용 도구 |
|---|---|
| Orchestrator | 전체 MCP 도구 + UI 함수 도구 + `consult_specialist` |
| CAD Change Interpreter | `spatial_*`, `revision_diff`, `revision_process`, `registry_impact` |
| Space Productivity Analyst | `spatial_*`, `analytics_*` |
| Layout Planner | `spatial_*`, `analytics_productivity`, `analytics_cross_shopping`, `layout_generate_alternatives`, `whatif_score_moves`, `revision_create_hypothetical` |
| Generator | `spatial_*`, `registry_list_generators`, `registry_run`, `render_adhoc_table` |
| QA Reviewer | `spatial_*`, `registry_validate`, `registry_impact` |
| Market & Benchmark Researcher | MCP 없음. 웹 검색만 사용 |

## 4. 공간 어휘

에이전트는 좌표를 보고 공간을 추론하지 않는다. 사람이 쓰는 공간 표현은 도면에서 결정론적으로 계산한 매장 속성(`cad2anything.spatial.vocabulary`)으로 바꿔 `spatial_query`와 `spatial_describe_floor`에서 쓴다.

| 필드 | 사람이 쓰는 말 | 계산 방법 |
|---|---|---|
| `side` | 동쪽, 북서쪽, 가운데 | 매장 중심이 층 외곽을 가로·세로 3등분한 9개 구역 중 어디에 있는지. 도면 기준이며 위쪽이 북이다. 필터의 `동`은 `북동`·`남동`도 포함한다. |
| `facade_directions`, `frontage_sides` | 두 면이 통로에 접한 매장 | 통로와 2m 이상 맞닿은 면의 바깥쪽 방향(동·북·서·남)과 그 수 |
| `corner` | 코너 매장 | 서로 직각인 두 면이 통로와 맞닿음 |
| `escalator_front`, `escalator_view_m`, `front_escalator` | 에스컬레이터 정면·앞, 에스컬레이터에서 보이는 매장 | 에스컬레이터 승하차 지점(긴 축의 양 끝)에서 매장의 통로 쪽 면까지 직선이 통로 안에만 있으면 보인다. 정면은 내리는 방향 45° 안, 20m 안이다. |
| `entrance_front`, `entrance_view_m` | 출입구 정면 매장 | 건물 출입구에서 같은 방법. 들어오는 방향 45° 안, 25m 안이다. |
| `traffic_potential`, `main_walkway` | 메인 동선, 사람이 많이 지나는 길 | 같은 층 수직동선·출입구 사이 최단 경로가 매장 앞(2.5m 안)을 지나는 정도(subset betweenness)를 층 최댓값 1로 나눈 값. 0.5 이상이면 `main_walkway`. 도면 구조로 계산한 잠재 유동이며 관측한 유동이 아니다. |
| `zone_name` | 란제리 구역, 식당가 | 도면의 존 이름 |
| `distance_m`(`near` 조건) | 에스컬레이터 근처, 걸어서 30m 안 | 통로망을 따라 걷는 거리. 매장은 통로에 접한 면의 가운데에서 잰다. |

**합성 도면에서 확인한 점:** N·S·E·W 매장 코드가 나타내는 방향과 `side`가 R2의 해당 매장 199개 모두에서 일치한다. 층마다 에스컬레이터 승하차 지점 앞 매장이 `escalator_front`로 잡히고, 여기에는 R1 개편에서 에스컬레이터 정면으로 옮긴 3F-C05가 포함된다.
