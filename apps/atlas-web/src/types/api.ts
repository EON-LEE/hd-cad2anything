// Types for the Atlas API contract (docs/specs/atlas-api.md).
import type { PatchOperation } from '../lib/jsonPatch'

// ---------------------------------------------------------------------------------------------
// GET /api/meta
// ---------------------------------------------------------------------------------------------

/** The contract does not pin the facility shape; accept a plain string or an object. */
export type Facility = string | { code?: string; name?: string; name_ko?: string }

export interface RevisionInfo {
  rev_id: string
  kind: 'actual' | 'hypothetical' | string
  parent_rev_id: string | null
  effective_date: string | null
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
  | { type: 'TOOL_CALL_START'; tool_call_id: string; tool_name: string; args?: unknown }
  | { type: 'TOOL_CALL_END'; tool_call_id: string; summary?: string }
  | { type: 'STATE_DELTA'; delta: PatchOperation[] }
  | { type: 'RUN_FINISHED'; thread_id: string; run_id: string }
  | { type: 'RUN_ERROR'; message: string }

export type AgUiEventType = AgUiEvent['type']

// ---------------------------------------------------------------------------------------------
// UI state model (target of STATE_DELTA patches)
// ---------------------------------------------------------------------------------------------

export type PanelType = 'changes' | 'artifact' | 'layout' | 'table' | 'none'

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

export interface Heatmap {
  metric: string
  label: string
  values: Record<string, number> | null
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
}
