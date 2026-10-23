// Types for the Atlas API contract (docs/specs/atlas-api.md).
import type { PatchOperation } from '../lib/jsonPatch'

// ---------------------------------------------------------------------------------------------
// GET /api/meta
// ---------------------------------------------------------------------------------------------

/** The contract does not pin the facility shape; accept a plain string or an object. */
export type Facility = string | { code?: string; name?: string; name_ko?: string }

export type ModelSource = 'parsed' | 'truth' | 'uploaded' | 'hypothetical'

export interface RevisionInfo {
  rev_id: string
  kind: 'actual' | 'hypothetical' | string
  parent_rev_id: string | null
  effective_date: string | null
  /**
   * Whether the actual revision's period has sales. `false` for an actual revision that starts after
   * `data_through` (its analytics are estimates, like a hypothetical revision). Absent on older backends.
   */
  has_sales?: boolean
  /** Model origin (`parsed`, `truth`, `uploaded`, `hypothetical`). Not the free-text `source` tool description. */
  model_source?: ModelSource | string
  /** Optional extension (e.g. "S/S MD 개편"); shown in the revision selector when present. */
  notes?: string
}

export interface FloorInfo {
  code: string
  ordinal: number
  name: string
}

export interface CategoryInfo {
  code: string
  name_ko: string
  color: string
}

export interface Meta {
  facility: Facility
  /** Last date with transaction data (YYYY-MM-DD); absent on older backends. */
  data_through?: string | null
  revisions: RevisionInfo[]
  floors: FloorInfo[]
  categories: CategoryInfo[]
}

// ---------------------------------------------------------------------------------------------
// GET /api/floors/{rev}/{floor}  (GeoJSON in local meters, not lon/lat)
// ---------------------------------------------------------------------------------------------

export type Position = number[]

export type Geometry =
  | { type: 'Point'; coordinates: Position }
  | { type: 'LineString'; coordinates: Position[] }
  | { type: 'MultiLineString'; coordinates: Position[][] }
  | { type: 'Polygon'; coordinates: Position[][] }
  | { type: 'MultiPolygon'; coordinates: Position[][][] }

export type FeatureKind =
  | 'unit'
  | 'walkway'
  | 'vertical_link'
  | 'amenity'
  | 'fixture'
  | 'exterior'
  | 'wall'
  | 'zone'
  | 'opening'

export interface FeatureProps {
  kind: FeatureKind | string
  unit_code?: string | null
  brand_id?: string | null
  brand_name?: string | null
  category?: string | null
  vacant?: boolean | null
  area_m2?: number | null
  zone_code?: string | null
  link_id?: string | null
  link_type?: string | null
  amenity_type?: string | null
  fixture_type?: string | null
  label?: string | null
  name_ko?: string | null
  [key: string]: unknown
}

export interface Feature {
  type: 'Feature'
  id?: string | number
  geometry: Geometry | null
  properties: FeatureProps
}

export interface FeatureCollection {
  type: 'FeatureCollection'
  features: Feature[]
}

// ---------------------------------------------------------------------------------------------
// GET /api/changes/{rev_from}/{rev_to}
// ---------------------------------------------------------------------------------------------

export type ChangeType =
  | 'add'
  | 'remove'
  | 'move'
  | 'resize'
  | 'reassign'
  | 'split'
  | 'merge'
  | 'fixture_change'
  | 'walkway_change'

export interface ChangeItem {
  change_type: ChangeType | string
  floor: string
  unit_codes_before: string[]
  unit_codes_after: string[]
  brand_ids_before?: string[]
  brand_ids_after?: string[]
  entity_ids_before?: string[]
  entity_ids_after?: string[]
  details?: Record<string, unknown>
  summary: string
}

export interface ChangeSet {
  rev_from: string
  rev_to: string
  items: ChangeItem[]
}

export interface ImpactEntry {
  generator: string
  title_ko: string
  risk_tier: 'high' | 'medium' | 'low' | string
  approval_mode: 'pre_publish' | 'post_publish' | string
  floors: string[]
  reasons: string[]
}

export interface ChangesResponse {
  change_set: ChangeSet
  impact: ImpactEntry[]
}

// ---------------------------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------------------------

export interface ArtifactFile {
  path: string
  url?: string | null
  [key: string]: unknown
}

export interface ArtifactIssue {
  severity: string
  message: string
  entity_ids?: string[]
}

export type ArtifactStatus = 'awaiting_approval' | 'published' | 'rejected' | 'qa_failed' | string

export interface Artifact {
  generator: string
  title_ko: string
  status: ArtifactStatus
  approval_mode: string
  files: ArtifactFile[]
  summary: unknown
  issues: (ArtifactIssue | string)[]
  reviewed_by?: string | null
  review_note?: string | null
}

export interface ReviewRequest {
  decision: 'approve' | 'reject'
  reviewer: string
  note: string
}

// ---------------------------------------------------------------------------------------------
// Analytics and layout
// ---------------------------------------------------------------------------------------------

export interface ProductivityRow {
  unit_code: string
  brand_name: string | null
  category: string | null
  sales_krw: number | null
  sales_per_m2: number | null
  sales_per_pyeong: number | null
  percentile: number | null
  low_efficiency: boolean
  driver: string | null
}

export interface ProductivityResponse {
  rows: ProductivityRow[]
  metric: { name: string; label: string; min: number; max: number }
}

export interface LayoutMove {
  brand_id: string
  brand_name: string
  from_unit: string
  to_unit: string
}

export interface LayoutAlternative {
  id: string
  moves: LayoutMove[]
  objective_before: number
  objective_after: number
  kpi_delta: { sales_krw: number; low: number; high: number }
  /** `detail` is an optional extension (layout engine's constraint check detail). */
  constraints: { name: string; ok: boolean; detail?: string }[]
  explanations: string[]
  /** Optional extension: hypothetical revision saved for this alternative. */
  rev_id?: string
}

export interface LayoutRequest {
  rev: string
  floors: string[]
  constraints: Record<string, unknown>
}

export interface LayoutResponse {
  alternatives: LayoutAlternative[]
}

// ---------------------------------------------------------------------------------------------
// Seasonal relayout ("시즌 개편"): /api/relayout/*
// ---------------------------------------------------------------------------------------------

export type RelayoutStatus = 'draft' | 'diagnosed' | 'planned' | 'selected' | 'checked'
export type RelayoutObjective = 'sales' | 'commission'
export type RelayoutMovable = 'expiring' | 'all'

/** One row of GET /api/relayout/briefs (`entries`/`exits` are counts). */
export interface RelayoutBriefSummary {
  brief_id: string
  title: string
  season: string
  year: number | null
  effective_date: string | null
  base_rev: string
  floors: string[]
  objective: RelayoutObjective | string
  status: RelayoutStatus | string
  entries: number
  exits: number
  updated_at: string | null
}

export interface RelayoutEntry {
  brand_id: string
  brand_name: string
  category: string | null
  category_name: string | null
  /** [min, max] in m²; null when the brief does not say. */
  target_area_m2: [number, number] | null
  preferred_floors: string[]
  commission_rate: number | null
  note: string | null
  /** True for a brand new to the store. */
  new_brand: boolean | null
  /** Brand drawing power used by the layout model (1 = average). */
  attractiveness: number | null
}

export interface RelayoutExit {
  brand_id: string
  brand_name: string
  unit_code: string | null
  reason: string | null
}

export interface RelayoutBrief {
  brief_id: string
  title: string
  season: string
  /** e.g. "F/W(9~2월)" */
  season_label: string | null
  year: number | null
  effective_date: string | null
  base_rev: string
  floors: string[]
  objective: RelayoutObjective | string
  /** e.g. "수수료 수익" */
  objective_label: string | null
  max_moves: number | null
  downtime_weeks: number | null
  movable: RelayoutMovable | string | null
  keep_anchors: boolean | null
  /** Allowed area difference for placing a brand (0.15 = ±15%). */
  area_tolerance: number | null
  entries: RelayoutEntry[]
  exits: RelayoutExit[]
  notes: string | null
}

export interface DatePeriod {
  start: string
  end: string
  days?: number | null
}

export interface RelayoutCandidate {
  unit_code: string
  floor: string | null
  brand_id: string | null
  brand_name: string | null
  category_name: string | null
  lease_end: string | null
  sales_per_m2: number | null
  /** 0..100. */
  percentile: number | null
  low_efficiency: boolean
  reasons: string[]
}

export interface RelayoutDiagnosis {
  season: { code: string; label: string; year: number | null; last: DatePeriod | null; previous: DatePeriod | null }
  /** Period of the efficiency figures (sales per m², percentiles). */
  period: DatePeriod | null
  /** Category's season daily sales / annual daily sales (above 1 = strong in the season). */
  season_factors: { category: string; category_name: string; factor: number }[]
  floors: { floor: string; sales_per_m2: number | null; yoy_pct: number | null; low_efficiency: number | null }[]
  candidates: RelayoutCandidate[]
  /** All candidates found (the list may be cut short). */
  candidate_total: number | null
  vacancies: { unit_code: string; floor: string | null; area_m2: number | null }[]
  entries: (RelayoutEntry & { feasible_units: number | null; feasible_unit_codes: string[]; season_factor: number | null })[]
  exits: RelayoutExit[]
  notes: string[]
}

export type RelayoutChangeType = 'move' | 'entry' | 'exit'

export interface RelayoutChange {
  type: RelayoutChangeType | string
  brand_id: string
  brand_name: string
  category_name: string | null
  /** Absent for an entry. */
  from_unit: string | null
  /** Absent for an exit. */
  to_unit: string | null
  from_floor: string | null
  to_floor: string | null
  delta_krw: number | null
  explanation: string | null
}

/** Changes against the plan `baseline` (exits and entries only), except the `vs_current_*` values. */
export interface RelayoutKpi {
  sales_delta_krw: number | null
  sales_low_krw: number | null
  sales_high_krw: number | null
  objective_delta_krw: number | null
  move_cost_krw: number | null
  /** objective_delta_krw - move_cost_krw */
  net_krw: number | null
  vs_current_sales_krw: number | null
  vs_current_objective_krw: number | null
  moves: number | null
}

export interface RelayoutAlternative {
  /** `ALT-0` is the baseline (exits and entries only, deltas 0); then ALT-1..n. */
  alt_id: string
  /** Hypothetical revision saved for this alternative (`{brief_id}-A{n}`). */
  rev_id: string | null
  summary: string
  kpi: RelayoutKpi
  changes: RelayoutChange[]
  constraints: { name: string; ok: boolean; detail?: string }[]
  explanations: string[]
}

export interface RelayoutPlanOptions {
  objective?: RelayoutObjective
  n_alternatives?: number
  max_moves?: number
  downtime_weeks?: number
  movable?: RelayoutMovable
  extra_exits?: string[]
  extra_entries?: string[]
}

export interface RelayoutPlan {
  params: Record<string, unknown>
  current: { sales_krw: number | null; objective_krw: number | null }
  baseline: { sales_krw: number | null; objective_krw: number | null; note: string | null }
  alternatives: RelayoutAlternative[]
  /** Entry brands that found no unit. */
  unplaced: { brand_id: string; brand_name: string; reason: string }[]
  caveats: string[]
}

export type RelayoutCheckStatus = 'match' | 'different_unit' | 'missing' | 'not_exited'

export interface RelayoutCheckItem {
  type: RelayoutChangeType | string
  /** Korean type name from the backend (e.g. "이전"); null when absent. */
  type_label: string | null
  brand_name: string
  from_unit: string | null
  planned_unit: string | null
  actual_unit: string | null
  status: RelayoutCheckStatus | string
  /** Korean status name from the backend (e.g. "다른 매장"); null when absent. */
  status_label: string | null
}

/** Planned alternative vs the actual drawing the CAD team uploaded. */
export interface RelayoutCheck {
  rev: string
  plan_rev: string | null
  alt_id: string | null
  conformance_pct: number | null
  items: RelayoutCheckItem[]
  /** Units whose brand changed outside the plan; `*_brand` are brand IDs, `*_brand_name` display names. */
  unexpected: {
    unit_code: string
    before_brand: string | null
    after_brand: string | null
    before_brand_name: string | null
    after_brand_name: string | null
  }[]
  area_changes: { unit_code: string; before_m2: number | null; after_m2: number | null; delta_pct: number | null }[]
  summary: string
}

/** `exit` 퇴점·철거, `move` 이전 공사, `entry` 신규 입점 공사, `temporary` 임시 매장 영업, `close` 임시 휴점. */
export type PhasingAction = 'exit' | 'move' | 'entry' | 'temporary' | 'close'

/** One work of the construction order (weeks count from the start of the works, week 0). */
export interface PhasingStep {
  step: number
  start_week: number
  end_week: number
  action: PhasingAction | string
  /** Korean action name from the backend (e.g. "이전 공사"); null when absent. */
  action_label: string | null
  /** Unit worked on: the new unit of a move or entry, the vacated unit of an exit, the temporary unit. */
  unit_code: string | null
  floor: string | null
  brand_id: string | null
  brand_name: string
  from_unit: string | null
  /** Step numbers that must finish first. */
  waits_for: number[]
  closed_weeks: number
  temporary_unit: string | null
  lost_krw: number | null
  note: string | null
}

export interface PhasingEntry {
  brand_id: string | null
  brand_name: string
  unit_code: string | null
  open_week: number | null
  /** Earliest week the brand could open after waiting for its chain of moves (with enough crews). */
  earliest_week: number | null
}

/** Brands moving into each other's units, resolved by a temporary unit or a closure. */
export interface PhasingCycle {
  brands: string[]
  units: string[]
  /** Brand that leaves its unit first. */
  breaker: string | null
  resolved_by: 'temporary' | 'close' | string
  /** Temporary unit (null when the brand closes). */
  unit_code: string | null
  weeks: number | null
}

/** POST /api/relayout/{brief_id}/phasing (also `project.phasing`). */
export interface RelayoutPhasing {
  brief_id: string
  alt_id: string | null
  rev_id: string | null
  params: { crews: number | null; fitout_weeks: number | null; temporary_sales_ratio: number | null; use_temporary: boolean | null }
  steps: PhasingStep[]
  total_weeks: number | null
  /** Expected sales lost to closures and temporary units. */
  lost_krw: number | null
  /** The plan's assumption: every moving brand closes for the fit-out weeks. */
  naive_lost_krw: number | null
  saved_krw: number | null
  /** Expected sales of new brands opening later than their `earliest_week` because crews are busy. */
  entry_delay_krw: number | null
  entries: PhasingEntry[]
  cycles: PhasingCycle[]
  summary: string
  caveats: string[]
  created_at: string | null
}

/** Omitted fields take the backend defaults (selected alternative, 2 crews, the brief's downtime weeks). */
export interface RelayoutPhasingRequest {
  alt_id?: string
  /** 1..6 */
  crews?: number
  /** 1..12 */
  fitout_weeks?: number
  /** 0..1 */
  temporary_sales_ratio?: number
  use_temporary?: boolean
}

/** GET /api/relayout/{brief_id}: diagnosis, plan, check and phasing are null until made. */
export interface RelayoutProject {
  brief: RelayoutBrief
  status: RelayoutStatus | string
  diagnosis: RelayoutDiagnosis | null
  plan: RelayoutPlan | null
  selected_alt_id: string | null
  decided_by: string | null
  decided_at: string | null
  check: RelayoutCheck | null
  phasing: RelayoutPhasing | null
  report_url: string | null
  updated_at: string | null
}

export interface RelayoutSelectRequest {
  alt_id: string
  decided_by: string
}

// ---------------------------------------------------------------------------------------------
// Store benchmark and relayout review: /api/analytics/stores, /api/analytics/relayout-review
// ---------------------------------------------------------------------------------------------

export type StoreLevel = 'store' | 'floor' | 'category'

export interface StoreInfo {
  store_id: string
  store_name: string
  region: string | null
  format: string | null
  /** The store this app analyzes (its figures come from transactions). */
  flagship: boolean
}

export interface StoreComparisonRow {
  key: string
  label: string
  /** Value of the flagship store. */
  flagship: number | null
  peer_median: number | null
  rank: number | null
  n_stores: number | null
  gap_pct: number | null
  /** store_id -> value */
  values: Record<string, number | null>
}

export interface StoreComparison {
  level: StoreLevel | string
  metric: string
  metric_label: string
  period: DatePeriod | null
  stores: StoreInfo[]
  rows: StoreComparisonRow[]
  notes: string[]
  caveat: string | null
}

export interface StoreComparisonParams {
  level?: StoreLevel
  metric?: string
  floor?: string | null
  category?: string | null
}

export interface RelayoutReviewItem {
  change_type: string
  unit_code: string
  floor: string | null
  brand_name: string | null
  before_daily_krw: number | null
  after_daily_krw: number | null
  change_pct: number | null
  /** Change of the unchanged units on the same floor (the control group). */
  control_pct: number | null
  vs_control_pct: number | null
}

/** Same-length windows before and after a revision's effective date (not a causal effect). */
export interface RelayoutReview {
  rev: string
  parent_rev: string | null
  effective_date: string | null
  window: { before: DatePeriod | null; after: DatePeriod | null; days: number | null }
  items: RelayoutReviewItem[]
  /** Groups by change type and by floor (shape not pinned by the contract). */
  by_type: Record<string, unknown>[]
  by_floor: Record<string, unknown>[]
  summary: string
  caveat: string | null
}

// ---------------------------------------------------------------------------------------------
// Drawing and metadata uploads: /api/uploads/*
// ---------------------------------------------------------------------------------------------

export type UploadJobStatus = 'queued' | 'running' | 'needs_review' | 'done' | 'failed'
export type UploadStepStatus = 'pending' | 'running' | 'done' | 'skipped' | 'failed'

export interface DrawingUploadRequest {
  /** Several DXF files (floor code in each name) or one ZIP. */
  files: File[]
  rev_id: string
  /** Default: latest actual revision with sales. */
  parent_rev?: string | null
  /** YYYY-MM-DD */
  effective_date: string
  /** Default: the profile's units. */
  units?: 'm' | 'mm' | null
  /** Default: `default`. */
  profile?: string | null
  /** Default: true. */
  run_generators?: boolean
}

export interface UploadJobCreated {
  job_id: string
  status: UploadJobStatus | string
}

export interface UploadJobSummary {
  job_id: string
  kind: string
  status: UploadJobStatus | string
  rev_id: string | null
  created_at: string | null
  updated_at: string | null
}

export interface UploadJobStep {
  name: string
  label: string
  status: UploadStepStatus | string
  message: string | null
}

/** A layer whose meaning the parser was not sure about. */
export interface LayerReviewItem {
  floor: string | null
  layer: string
  semantic: string | null
  /** 0..1 */
  confidence: number | null
  alternatives: string[]
  samples: string[]
  question: string | null
}

export interface UploadQaIssue {
  severity: string
  code: string
  unit_code: string | null
  message: string
}

export interface UploadJob extends UploadJobSummary {
  parent_rev: string | null
  effective_date: string | null
  profile: string | null
  files: { name: string; floor: string | null; bytes: number | null }[]
  floors_uploaded: string[]
  floors_inherited: string[]
  steps: UploadJobStep[]
  parse: { units: number | null; issues: { error: number; warning: number; info: number }; review_queue: LayerReviewItem[] } | null
  changes: { total: number; by_type: Record<string, number> } | null
  impact: { generator: string; title_ko: string; risk_tier: string }[]
  artifacts: { generator: string; status: string }[]
  qa: { errors: number; warnings: number; infos: number; issues: UploadQaIssue[] } | null
  relayout_check: { brief_id: string; alt_id: string | null; conformance_pct: number | null; summary: string } | null
  error: string | null
}

export interface LayerMapping {
  layer: string
  semantic: string
  floor?: string | null
}

export interface DrawingProfile {
  profile_id: string
  title: string
  description: string | null
  units: string | null
}

export type MetadataKind =
  | 'brands'
  | 'tenancies'
  | 'leases'
  | 'pos_terminals'
  | 'relayout_brief'
  | 'drawing_profile'
  | 'stores'
  | 'store_kpis'

/** POST /api/uploads/metadata (rejections also come back as 200 with `errors`). */
export interface MetadataUploadResult {
  kind: MetadataKind | string
  status: 'accepted' | 'rejected' | string
  rows: number | null
  columns: string[]
  errors: string[]
  warnings: string[]
  /** Where the previous file was kept before replacing the table. */
  backup: string | null
  /** Brief or profile ID. */
  id: string | null
}

export interface MetadataStatus {
  kind: MetadataKind | string
  present: boolean
  rows: number | null
  updated_at: string | null
  required_columns: string[]
  /** Brief or profile IDs (empty for tables). */
  items: string[]
}

// ---------------------------------------------------------------------------------------------
// GET /api/analytics/journey  (one customer's purchase sequence on one day; also UI state `journey`)
// ---------------------------------------------------------------------------------------------

export interface JourneyStop {
  order: number
  /** "HH:MM" of the purchase. */
  time: string
  unit_code: string
  floor: string
  brand_name: string | null
  category: string | null
  category_name: string | null
  amount_krw: number | null
  /** Minutes since the previous stop (null for the first stop). */
  gap_min: number | null
  /** Store entrance (where the unit meets the walkway), local meters; null when unknown. */
  x: number | null
  y: number | null
  [key: string]: unknown
}

export interface JourneySegment {
  floor: string
  points: [number, number][]
  from_order: number | null
  to_order: number | null
}

export interface JourneyTransition {
  from_order: number | null
  to_order: number | null
  from_floor: string
  to_floor: string
  /** Vertical link used (e.g. "ESC-A"); null when unknown. */
  via: string | null
}

export interface JourneySummary {
  stops: number
  floors: string[]
  total_krw: number | null
  duration_min: number | null
  walk_m: number | null
}

/** Segment filter as an object (`{age_band: "40s", tier: ["VIP", "VVIP"]}`) or the string form. */
export type SegmentFilter = Record<string, unknown> | string

export interface Journey {
  title: string
  customer: { id: string; segment: string } | null
  date: string | null
  weekday: string | null
  rev: string | null
  query: { segment: SegmentFilter | null; rank: number; rev?: string | null } | null
  stops: JourneyStop[]
  segments: JourneySegment[]
  transitions: JourneyTransition[]
  summary: JourneySummary
  caveat: string | null
  [key: string]: unknown
}

export interface JourneyParams {
  customer_id?: string | null
  date?: string | null
  segment?: SegmentFilter | null
  rank?: number | null
  /** Choose among days when this revision was the store layout. */
  rev?: string | null
}

// ---------------------------------------------------------------------------------------------
// GET /api/spatial/route  (shortest walkway route between two places; also UI state `route`)
// ---------------------------------------------------------------------------------------------

export type RoutePlaceKind = 'unit' | 'entrance' | 'escalator' | 'elevator' | 'stairs' | 'amenity'

export interface RoutePlace {
  /** Unit code ("3F-C05"), entrance id ("ENT-1") or floor-qualified link/amenity ("1F:ESC-A"). */
  ref: string
  kind: RoutePlaceKind | string
  label: string
  floor: string
  unit_code: string | null
  /** Access point (where the place meets the walkway), local meters; null when unknown. */
  x: number | null
  y: number | null
  [key: string]: unknown
}

export interface RouteSegment {
  floor: string
  points: [number, number][]
  [key: string]: unknown
}

export interface RouteTransition {
  from_floor: string
  to_floor: string
  /** Vertical link used (e.g. "ESC-A"); null when unknown. */
  via: string | null
  /** Number of floors moved; null when unknown. */
  floors: number | null
  /** Sum of the floor heights moved, in meters; null when unknown. */
  height_m: number | null
  [key: string]: unknown
}

export interface Route {
  title: string
  rev: string | null
  /** "escalator" or "elevator"; null when unknown. */
  via_type: string | null
  origin: RoutePlace
  destination: RoutePlace
  /** One walking line per floor (origin floor, then destination floor). */
  segments: RouteSegment[]
  transitions: RouteTransition[]
  /** Walking distance on the origin and destination floors (rounded to 5 m); null when unknown. */
  walk_m: number | null
  caveat: string | null
  [key: string]: unknown
}

// ---------------------------------------------------------------------------------------------
// GET /api/spatial/flow  (aggregates drawn on the walkways; also UI state `flow`)
// ---------------------------------------------------------------------------------------------

/** `traffic` 추정 유동, `popup` 팝업 후보, `egress` 통로 폭·피난 거리 점검. */
export type FlowMode = 'traffic' | 'popup' | 'egress'

/** `spot` 통로 위 공간, `unit` 공실·팝업 매장, `warning` 피난 거리 초과 지점. */
export type FlowMarkerKind = 'spot' | 'unit' | 'warning'

export interface FlowSegment {
  points: [number, number][]
  /** 0..1 share of the floor maximum; derived from value / max_value when absent; null when unknown. */
  weight: number | null
  /** Estimated customers (traffic, popup) or the walkway width in meters (egress). */
  value: number | null
  /** Color chosen by the backend (e.g. the egress warning color); null = color by weight. */
  color: string | null
  label: string | null
  [key: string]: unknown
}

export interface FlowFloor {
  segments: FlowSegment[]
  max_value: number | null
  total: number | null
  [key: string]: unknown
}

export interface FlowMarker {
  floor: string
  x: number
  y: number
  rank: number | null
  kind: FlowMarkerKind | string
  /** Text inside the marker (e.g. the rank). */
  label: string | null
  title: string | null
  unit_code: string | null
  [key: string]: unknown
}

export interface FlowLegend {
  low: string | null
  high: string | null
  unit: string | null
  note: string | null
}

export interface FlowSummary {
  customer_days: number | null
  trips: number | null
  floors: string[]
  [key: string]: unknown
}

export interface Flow {
  mode: FlowMode | string
  title: string
  rev: string | null
  /** Request echo (segment, day_type, hours, floor, ...). */
  query: Record<string, unknown> | null
  /** floor code -> segments on that floor */
  floors: Record<string, FlowFloor>
  markers: FlowMarker[]
  legend: FlowLegend | null
  summary: FlowSummary
  caveat: string | null
  [key: string]: unknown
}

export interface FlowParams {
  mode: FlowMode
  rev?: string | null
  floor?: string | null
  segment?: SegmentFilter | null
  day_type?: 'all' | 'weekday' | 'weekend' | null
  /** [start, end] hours or the query form "10-14". */
  hours?: [number, number] | string | null
  category?: string | null
  min_width_m?: number | null
  max_travel_m?: number | null
}

// ---------------------------------------------------------------------------------------------
// POST /api/chat (AG-UI compatible SSE)
// ---------------------------------------------------------------------------------------------

export interface ChatUiState {
  rev: string
  floor: string
  selection: string[]
}

export interface ChatRequest {
  thread_id?: string
  message: string
  ui_state: ChatUiState
}

export type AgUiEvent =
  | { type: 'RUN_STARTED'; thread_id: string; run_id: string }
  | { type: 'TEXT_MESSAGE_START'; message_id: string }
  | { type: 'TEXT_MESSAGE_CONTENT'; message_id: string; delta: string }
  | { type: 'TEXT_MESSAGE_END'; message_id: string }
  | {
      type: 'TOOL_CALL_START'
      tool_call_id: string
      tool_name: string
      args?: unknown
      /** User-facing Korean description (e.g. "4층 공간 생산성 계산"). */
      label?: string
      /** Set for calls made inside a specialist agent (the specialist's consult call). */
      parent_tool_call_id?: string
      agent?: string
    }
  | {
      type: 'TOOL_CALL_END'
      tool_call_id: string
      /** Raw developer text; never shown by default. */
      summary?: string
      /** Replaces the label from TOOL_CALL_START when present. */
      label?: string
      ok?: boolean
    }
  | { type: 'STATE_DELTA'; delta: PatchOperation[] }
  | { type: 'RUN_FINISHED'; thread_id: string; run_id: string }
  | { type: 'RUN_ERROR'; message: string }

export type AgUiEventType = AgUiEvent['type']

// ---------------------------------------------------------------------------------------------
// UI state model (target of STATE_DELTA patches)
// ---------------------------------------------------------------------------------------------

export type PanelType = 'changes' | 'artifact' | 'layout' | 'table' | 'journey' | 'uploads' | 'none'

export interface Highlight {
  unit_code: string
  color?: string
  reason?: string
}

export interface GraphEdge {
  from: string
  to: string
  weight?: number
  label?: string
  color?: string
}

export interface HeatmapLegend {
  /** Two-valued metric (for example a low-efficiency flag): show two swatches instead of a gradient. */
  binary?: boolean
  low?: string
  high?: string
  note?: string
}

export interface Heatmap {
  metric: string
  label: string
  values: Record<string, number> | null
  legend?: HeatmapLegend
}

export interface LayerToggles {
  fixtures: boolean
  walkways: boolean
  labels: boolean
}

export interface UiState {
  rev: string
  compareRev: string | null
  floor: string
  layers: LayerToggles
  heatmap: Heatmap | null
  highlights: Highlight[]
  edges: GraphEdge[]
  focus: { unit_codes: string[] } | null
  panel: { type: PanelType; payload: Record<string, unknown> } | null
  /** One customer's shopping path (drawn on the floors it visits); null when off. */
  journey: Journey | null
  /** Shortest walking route between two places (drawn on its floors); null when off. */
  route: Route | null
  /** Aggregates on the walkways (estimated traffic, popup candidates, egress check); null when off. */
  flow: Flow | null
}
