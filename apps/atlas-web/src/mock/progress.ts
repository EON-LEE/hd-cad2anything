// The "분석 과정" text of mock runs. Method sentences and specialist results are copied from the backend
// (src/cad2anything/agents/progress.py: TOOL_METHODS, SPECIALIST_METHODS, specialist_result) so that mock mode
// reads like a live backend. Keep them in sync when the backend wording changes.

export const TOOL_METHODS: Readonly<Record<string, string>> = {
  spatial_list_revisions: '도면 개정 목록과 적용일을 확인해요',
  spatial_floor_summary: '층의 매장 수·면적·카테고리 구성을 집계해요',
  spatial_find_units: '조건에 맞는 매장을 도면 속성으로 찾아요',
  spatial_unit_details: '인접 매장·통로 접면·에스컬레이터까지 보행거리를 도면에서 읽어요',
  spatial_nearby_units: '매장 경계에서 반경 안의 매장을 거리순으로 찾아요',
  spatial_query: '도면에서 계산한 매장 속성으로 거르고 묶어 집계해요',
  spatial_route: '1m 간격 통로망에서 최단 경로를 찾아 걷는 거리를 재요',
  spatial_describe_floor: '수직동선·구역·주요 매장의 위치를 도면 기준 방위로 정리해요',
  spatial_popup_candidates: '추정 유동·승강장 가시성·남는 통로 폭으로 팝업 자리에 점수를 매겨요',
  spatial_egress_check: '통로 폭과 계단·출입구까지 걷는 거리를 도면으로 재요',
  revision_diff: '두 개정을 매장 단위로 비교해 이동·교체·면적 변경을 찾아요',
  revision_process: '올린 도면을 읽어 부모 개정과 비교하고 산출물을 다시 만들어요',
  revision_create_hypothetical: '브랜드 이동안으로 가상 도면을 만들어요',
  registry_list_generators: '등록된 산출물 생성기를 확인해요',
  registry_impact: '바뀐 도면 때문에 다시 만들 산출물과 승인 방식을 찾아요',
  registry_run: '생성기로 산출물 초안을 만들어요',
  registry_validate: '산출물 검증 결과를 확인해요',
  render_adhoc_table: '도면 데이터로 즉석 표 초안을 만들어요',
  analytics_productivity: '결제 실적을 연 매출로 환산해 ㎡당 매출을 구하고 층 하위 10%를 저효율로 봐요',
  analytics_drivers: '㎡당 매출을 노출·통로 접면·면적·카테고리로 회귀 분해해요',
  analytics_cross_shopping: '같은 고객이 같은 날 함께 산 비율이 평소의 몇 배인지 계산해요',
  analytics_floor_transitions: '같은 날 결제 순서로 층 사이 이동 비율을 계산해요',
  analytics_adjacency_effect: '근처 매장 효과를 에스컬레이터 노출을 통제한 비교로 다시 봐요',
  analytics_before_after: '변경일 전후 같은 기간 매출을 같은 층 비교군과 비교해요',
  analytics_time_patterns: '시간대·요일별 매출과 비중을 집계해요',
  analytics_customer_journey: '가명 고객 한 명의 하루 결제 순서를 찾아요',
  analytics_flow_map: '같은 날 이어진 결제를 통로망 최단 경로로 이어 통로별로 모아요',
  analytics_store_compare: '우리 점포 실적과 다른 점포 실적 자료를 같은 기준으로 비교해요',
  analytics_relayout_review: '바뀐 매장의 전후 매출을 같은 층의 바뀌지 않은 매장과 비교해요',
  layout_generate_alternatives: '제약을 지키며 기대 매출이 가장 커지는 배치를 정수계획으로 찾아요',
  whatif_score_moves: '이동안의 기대 매출 변화를 점수 모델로 추정해요',
  relayout_list_briefs: '사람이 올린 개편 브리프와 진행 상태를 확인해요',
  relayout_diagnose: '시즌 지수·계약 만료·저효율로 개편 후보를 진단해요',
  relayout_plan: '기대 매출에서 이전 공사 손실을 뺀 순효과가 가장 큰 배치를 정수계획으로 찾아요',
  relayout_select: '사람이 고른 대안과 결정자를 기록해요',
  relayout_check_drawing: '선택한 대안과 올린 도면을 매장별로 대조해요',
  relayout_report: '진단·대안·공사 순서를 제안서로 묶어요',
  relayout_entry_options: '빈 매장마다 신규 브랜드의 기대 매출을 추정해 비교해요',
  relayout_unit_options: '이 자리에 카테고리별 평균 브랜드를 넣었을 때 기대 매출을 비교해요',
  relayout_phasing: '빈 매장부터 공사하고 공사팀 수에 맞춰 순서와 영업 손실을 계산해요',
  upload_list_jobs: '올린 도면의 처리 단계와 결과를 확인해요',
  revision_check_metadata: '도면과 계약 정보를 매장별로 대조해요',
  data_provenance: '기본 데모 데이터와 사람이 올린 자료의 출처를 영역별로 확인해요',
  ui_set_floor: '도면을 해당 층으로 옮겨요',
  ui_show_heatmap: '지표 값으로 매장을 색칠해요',
  ui_highlight_units: '근거가 되는 매장을 강조해요',
  ui_draw_edges: '매장·카테고리 사이 관계를 선으로 그려요',
  ui_focus: '해당 매장으로 화면을 확대해요',
  ui_compare: '두 개정의 차이를 도면에 표시해요',
  ui_show_panel: '결과 화면을 준비해요',
  ui_show_journey: '결제 순서를 통로망 최단 경로로 이어 도면에 그려요',
  ui_show_route: '최단 경로를 도면에 그려요',
  ui_show_flow: '통로 위에 집계 결과를 그려요',
  ui_clear: '화면 표시를 지워요',
  consult_specialist: '전문 에이전트에게 여러 단계의 분석을 맡겨요',
  web_search: '공개 웹 자료를 검색해요',
  code_interpreter: '계산 코드를 실행해 숫자를 확인해요',
}

export const SPECIALIST_METHODS: Readonly<Record<string, string>> = {
  cad_change_interpreter: '도면 변경 해석 에이전트가 바뀐 점과 다시 만들 산출물을 정리해요',
  space_productivity_analyst: '공간 생산성 분석 에이전트가 여러 지표로 원인을 깊게 분석해요',
  layout_planner: '레이아웃 설계 에이전트가 배치 대안을 만들고 비교해요',
  generator: '산출물 생성 에이전트가 표·문서 초안을 만들어요',
  qa_reviewer: '검증 에이전트가 결과의 오류와 누락을 점검해요',
  researcher: '시장 조사 에이전트가 공개 자료를 찾아 출처와 함께 요약해요',
}

export const SPECIALIST_LABELS: Readonly<Record<string, string>> = {
  cad_change_interpreter: '도면 변경 해석 에이전트',
  space_productivity_analyst: '공간 생산성 분석 에이전트',
  layout_planner: '레이아웃 설계 에이전트',
  generator: '산출물 생성 에이전트',
  qa_reviewer: '검증 에이전트',
  researcher: '시장 조사 에이전트',
}

/** How a tool call computes its answer, or null for tools without a sentence (backend `tool_method`). */
export function toolMethod(name: string, args?: unknown): string | null {
  if (name === 'consult_specialist' && typeof args === 'object' && args !== null) {
    const method = SPECIALIST_METHODS[String((args as Record<string, unknown>).agent)]
    if (method) return method
  }
  return TOOL_METHODS[name] ?? null
}

/** Result line of a specialist consultation (backend `specialist_result`). */
export function specialistResult(agentKey: string, ok = true): string {
  const who = SPECIALIST_LABELS[agentKey] ?? '전문 에이전트'
  return ok ? `${who}의 분석을 받았어요` : `${who}의 답을 받지 못해 직접 정리해요`
}
