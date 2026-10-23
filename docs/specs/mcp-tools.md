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
| spatial | `spatial_popup_candidates` | `rev?`, `day_type?`(기본 `weekend`), `hours?`, `segment?`, `floor?`, `category?`, `min_area_m2?`, `top?` | 팝업 후보(통로 위 공간·공실·팝업 매장): 층, 위치 설명, 하루 추정 통행, 승강장에서 보이는 거리, 점수와 이유. `category`를 주면 함께 잘 팔리는 카테고리 매장이 가까운 곳에 가점 |
| spatial | `spatial_egress_check` | `rev?`, `floor?`, `min_width_m?`(기본 2.4), `max_travel_m?`(기본 50), `top?` | 층별 최소·중앙 통로 폭, 기준보다 좁은 통로 구간, 계단·출입구까지 걷는 거리가 기준을 넘는 매장, 통로로 이어지지 않는 매장. 검토용 예시 기준이며 법규 판정이 아님 |
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
| analytics | `analytics_flow_map` | `rev?`, `segment?`, `floor?`, `day_type?`, `hours?`, `start?`, `end?`, `top?` | 고객군의 통로별 추정 유동: 많이 지난 통로(기간 합계, 하루 평균, 층 이동 중 비율), 앞을 많이 지나는 매장, 층별 이동 수. 같은 날 연속한 두 결제 매장을 통로망 최단 경로로 이은 추정이며 관측 유동이 아님 |
| analytics | `analytics_store_compare` | `level?`(`store`·`floor`·`category`), `metric?`(`sales_per_m2`·`sales_per_pyeong`·`sales_krw`·`area_m2`·`growth_pct`), `floor?`, `category?`, `start?`, `end?`, `top?` | 점포별 값, 다른 점포 중앙값, 순위, 중앙값 대비 차이(`gap_pct`), 한국어 요약. 대상 점포는 거래와 그달의 도면, 다른 점포는 `store_kpis` 메타데이터에서 계산한다. 기본 기간은 최근 12개월이다. |
| analytics | `analytics_relayout_review` | `rev`, `window_days?` | 개정의 바뀐 매장마다 적용일 앞뒤 같은 기간의 일평균 매출 변화와 같은 층 비교군 대비 차이, 변경 유형별·층별·전체 합계, 보고서 링크. 인과 아님 |
| relayout | `relayout_list_briefs` | — | 사람이 올린 개편 브리프와 프로젝트 진행 상태(`draft`·`diagnosed`·`planned`·`selected`·`checked`), 선택된 대안 |
| relayout | `relayout_diagnose` | `brief_id` | 카테고리 시즌 지수, 대상 층의 ㎡당 매출과 1년 전 같은 시즌 대비 증감, 계약 만료·저효율 후보, 공실, 입점 브랜드별 들어갈 수 있는 매장 수 |
| relayout | `relayout_plan` | `brief_id`, `objective?`, `n_alternatives?`, `max_moves?`, `downtime_weeks?`, `movable?`, `extra_exits?`, `extra_entries?` | 기준안(ALT-0)과 대안(ALT-1~)별 변경(입점·퇴점·이전), 순효과·목표 변화·이전 비용·기대 매출 변화(추정치), 제약 충족표, 가상 개정 ID(`{brief_id}-A{n}`) |
| relayout | `relayout_select` | `brief_id`, `alt_id`, `decided_by?` | 사람이 고른 대안을 기록한 프로젝트 |
| relayout | `relayout_check_drawing` | `brief_id`, `rev`, `alt_id?` | 계획한 변경마다 일치·다른 매장·도면에 없음·퇴점 안 됨, 일치율, 계획에 없는 변경, 3% 넘는 면적 변화 |
| relayout | `relayout_report` | `brief_id` | 개편 제안서(HTML) 링크와 포함한 절 |
| relayout | `relayout_entry_options` | `brand?`, `category?`, `rev?`, `floors?`, `season?`, `include_expiring?`, `top?` | 신규 브랜드를 넣을 공실·계약 만료 예정 매장 후보와 기대 매출 범위(추정치), 매장 특징 |
| relayout | `relayout_unit_options` | `unit_code`, `rev?`, `season?`, `objective?`, `include_other_floors?`, `top?` | 매장 하나(공실·퇴점 예정)에 맞는 카테고리 순위(평균 브랜드의 기대 매출·수수료 수익, 범위, 지금 브랜드 대비 비율), 카테고리별 미입점 브랜드 후보, 제외한 카테고리와 이유(추정치) |
| relayout | `relayout_phasing` | `brief_id`, `alt_id?`, `crews?`, `fitout_weeks?`, `temporary_sales_ratio?`, `use_temporary?` | 대안의 공사 순서: 주차별 단계(퇴점, 이전 공사, 입점 공사, 임시 매장, 휴점)와 선행 단계, 총 기간, 휴점 손실, 모두 휴점 가정 손실, 입점 지연 기회 손실, 입점 개장 주차, 순환 해소 방법. 프로젝트에 저장한다. |
| upload | `upload_list_jobs` | `job_id?`, `limit?` | 최근 도면 업로드 작업 목록, 또는 작업 하나의 단계·검토 큐·변경·계약 정보 대조·개편안 대조 요약 |
| upload | `revision_check_metadata` | `rev`, `floors?` | 도면과 계약 정보(`leases`)의 차이: 계약 매장이 도면에 없음, 브랜드 불일치, 면적 3% 초과 차이, 계약 없는 매장 |

금액은 합성 표본 거래에 표본 가중치를 곱한 전체 규모 값이다. 실적 지표는 개정 적용 기간의 합계를 연 단위로 환산한다. 실제 개정의 분석 기간은 그 개정의 적용일부터 다음 실제 개정 적용일 전날까지다. 적용일이 거래 데이터의 마지막 날 뒤인 실제 개정(예: 다음 시즌용으로 올린 도면)은 실적이 없다. 이 개정에서 `analytics_productivity`는 오류를 돌려주고, `spatial_query`는 가상 개정처럼 기대 매출(`expected_sales_krw`, 추정치)을 쓴다.

## 3. 에이전트별 허용 도구

| 에이전트 | 허용 도구 |
|---|---|
| Orchestrator | 전체 MCP 도구 + UI 함수 도구 + `consult_specialist` |
| CAD Change Interpreter | `spatial_*`, `revision_diff`, `revision_process`, `registry_impact`, `upload_list_jobs`, `revision_check_metadata`, `relayout_check_drawing` |
| Space Productivity Analyst | `spatial_*`, `analytics_*` |
| Layout Planner | `spatial_*`, `analytics_productivity`, `analytics_cross_shopping`, `layout_generate_alternatives`, `whatif_score_moves`, `revision_create_hypothetical`, `relayout_list_briefs`, `relayout_diagnose`, `relayout_plan`, `relayout_entry_options`, `relayout_unit_options`, `relayout_phasing`, `relayout_report` |
| Generator | `spatial_*`, `registry_list_generators`, `registry_run`, `render_adhoc_table` |
| QA Reviewer | `spatial_*`, `registry_validate`, `registry_impact`, `revision_check_metadata`, `upload_list_jobs` |
| Market & Benchmark Researcher | MCP 없음. 웹 검색만 사용 |

대안 결정(`relayout_select`)은 Orchestrator만 쓸 수 있다. Orchestrator는 사용자가 대안을 분명히 고른 경우에만 호출한다.

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

## 5. 시즌 개편 계산

개편 대안은 LLM이 고르지 않는다. `relayout_plan`이 아래 최적화 문제를 결정론적 탐색(탐욕 개선 → 모의 담금질 3,000회 → 탐욕 마무리, 시드 고정)으로 푼다. 결과는 찾은 배치 중 가장 좋은 것이며 최적해를 증명하지 않는다.

**입력(사람이 정함):** 개편 브리프(시즌, 적용일, 기준 개정, 대상 층, 목적, 최대 이동 수, 공사 기간, 이동 대상, 입점·퇴점 브랜드), 계약 정보(`leases`), 브랜드 카탈로그(목표 면적, 선호 층, 상충 카테고리).

**목적 함수:**

$$\max \sum_{b} w_b \, S(b, u_b) \;-\; \sum_{b \in \text{이전}} w_b \, S(b, u_b^{0}) \cdot \frac{\text{공사 주}}{52}$$

- $S(b, u)$: 브랜드 $b$를 매장 $u$에 둘 때의 연 기대 매출. 기존 [what-if 점수 모델](../../src/cad2anything/layout/whatif.py)(면적 탄력성, 에스컬레이터 보행거리, 인접 교차구매 보너스)이다.
- $w_b$: 브랜드 가중치 = 카테고리 시즌 지수 × (목적이 수수료 수익이면 수수료율, 매출이면 1).
- 시즌 지수 = 카테고리의 해당 시즌 월 일평균 매출 ÷ 연간 일평균 매출(2년 거래).
- 둘째 항은 이전 공사 기간에 잃는 매출(가중치 적용)이다. 입점 브랜드에는 붙지 않는다.

**제약:** 대상 층만 바꾼다. 앵커는 고정한다. 이동 대상(`movable=expiring`)은 적용일 전에 계약이 끝나는 브랜드다. 이동 수는 `max_moves` 이하다. 옮긴 브랜드와 입점 브랜드는 목표 면적 ±허용 오차와 선호 층을 지킨다. 바뀐 인접 쌍에 상충 카테고리가 없어야 한다.

**절차:**

1. 퇴점 브랜드를 빼고, 입점 브랜드를 가중 기대 매출이 가장 큰 빈 매장부터 넣는다. 이것이 기준안(ALT-0)이다.
2. 기준안에서 위 문제를 풀어 대안(ALT-1~)을 만든다. 두 번째 대안부터는 앞 대안과 같은 이동에 벌점을 줘서 서로 다른 안을 낸다. 입점 브랜드의 위치 조정은 이동 수에 넣지 않는다.
3. 대안마다 기준안 대비 순효과, 목표 변화, 이전 비용, 기대 매출 변화와 범위, 지금 배치 대비 매출 변화, 제약 충족표를 계산하고 가상 개정으로 저장한다.

사람은 대안 하나를 고른다(`relayout_select`). CAD팀이 그 대안대로 도면을 그려 올리면 `relayout_check_drawing`이 계획과 도면을 대조한다.

### 5.1 빈 자리 채우기(`relayout_unit_options`)

매장 하나를 비워 두고, 카테고리마다 "카테고리 평균 브랜드"를 그 자리에 넣었을 때의 연 기대 매출을 같은 점수 모델로 계산한다. 평균 브랜드는 그 카테고리 브랜드들의 목표 면적 중앙값, 가장 많이 선호하는 층과 브랜드의 20% 이상이 선호하는 층, 가장 흔한 상충 카테고리로 만들고 매력도는 기본값을 쓴다. 면적이 허용 오차를 벗어나거나 선호 층이 아니거나(`include_other_floors`로 완화) 옆 매장과 상충하는 카테고리는 제외하고 이유를 준다. `objective=commission`이면 카테고리 수수료율 중앙값(입점 브랜드 기준, 없으면 카탈로그 범위의 가운데)을 곱해 순위를 매긴다. 카테고리마다 아직 입점하지 않은 카탈로그 브랜드를 같은 방법으로 계산해 상위 3개를 붙인다.

### 5.2 공사 순서(`relayout_phasing`)

공사 순서도 LLM이 정하지 않는다. 대안의 이전·입점 변경마다 대상 매장의 공사 작업 하나를 만들고 아래 규칙으로 일정을 짠다.

1. **선행 관계:** 대상 매장에 지금 있는 브랜드가 함께 옮기는 브랜드이면, 그 브랜드의 새 매장 공사가 끝나야 공사를 시작할 수 있다. 공실과 퇴점 매장은 0주차부터 비어 있다. 매장마다 선행 작업이 최대 하나라서 선행 관계는 사슬과 순환으로만 이루어진다.
2. **순환 풀기:** 서로의 자리로 옮기는 순환에서는 주 매출이 가장 적은 브랜드가 먼저 자리를 비운다. 그 자리의 공사가 시작될 때 나가서, 자기 새 매장 공사가 끝날 때 다시 연다. 그동안 그 기간에 공사하지 않는 공실·퇴점 매장을 임시 매장으로 쓰면 평소 매출의 `temporary_sales_ratio`로 영업하고, 없으면 휴점한다.
3. **일정:** 공사팀 `crews`개가 비는 대로 시작할 수 있는 작업을 고른다(리스트 스케줄링). 이미 자리를 비운 브랜드의 사슬을 먼저 끝낸다. 그다음 우선순위는 세 가지로 짜 본다: 기다리는 작업이 많은 사슬 먼저, 끝나면 열리는 입점 매출이 큰 사슬 먼저, 남은 작업 하나당 입점 매출이 큰 사슬 먼저.
4. **선택:** 휴점·임시 매장 손실과 입점 지연 기회 손실의 합이 가장 작은 일정을 고르고, 같으면 총 기간이 짧은 일정을 고른다.

손실은 기준 개정의 연 매출을 52주로 나눈 주 매출로 계산한다. `naive_lost_krw`는 옮기는 브랜드가 모두 공사 기간만큼 휴점한다고 본 값(대안 계산의 가정)이다. 공사 기간은 매장마다 같고, 이사·진열 기간과 임시 매장 공사비는 넣지 않는다.
