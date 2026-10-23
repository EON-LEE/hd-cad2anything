// Mock drawing and metadata uploads behind /api/uploads/*. Drawing jobs run one at a time and advance
// on every poll of GET /api/uploads/jobs/{job_id} (two steps per poll), so the UI's polling drives them.
// The mock does not read DXF/ZIP contents: a drawing whose parent is R2 gets a prepared 3F change set
// (the F/W relayout as the CAD team drew it); other parents are copied as they are.
import { ApiError } from '../api/client'
import { isRecord } from '../lib/coerce'
import { LAYER_SEMANTICS, METADATA_KINDS, STEP_LABELS, STEP_ORDER, floorFromFileName, metadataKindInfo, validateDrawingUpload } from '../lib/uploads'
import type {
  DrawingProfile,
  DrawingUploadRequest,
  LayerMapping,
  LayerReviewItem,
  MetadataKind,
  MetadataStatus,
  MetadataUploadResult,
  UploadJob,
  UploadJobCreated,
  UploadJobSummary,
} from '../types/api'
import { planImpact } from './analytics'
import { MOCK_FLOORS } from './catalog'
import { kstNow, mockLeaseEnd, type MockRelayout } from './relayout'
import { buildUploadedRevision, changesBetween, rectArea, type MockWorld } from './world'

/** Parent revision for which mock uploads apply the prepared F/W relayout drawing. */
const SEASON_BASE_REV = 'R2'
const ACTIONS_PER_POLL = 2
const UNITS_PER_UNKNOWN_FLOOR = 18

export const MOCK_PROFILES: readonly DrawingProfile[] = [
  {
    profile_id: 'default',
    title: '기본 도면 규약',
    description: 'CAD 규약 문서의 레이어 이름(A-UNIT, A-WALK 등)과 m 단위를 쓰는 도면',
    units: 'm',
  },
  {
    profile_id: 'customer_style',
    title: '고객사 도면 스타일',
    description: '고객사 CAD팀의 레이어 이름과 mm 단위를 쓰는 도면. 레이어 매핑이 저장되어 있어 검토 질문이 없습니다.',
    units: 'mm',
  },
]

interface JobState {
  job: UploadJob
  /** Layer questions asked at the parse step (unmapped layers only). */
  questions: LayerReviewItem[]
  /** File that fails to parse (names containing "broken"). */
  broken: string | null
  runGenerators: boolean
}

const settled = (status: string) => status === 'done' || status === 'failed' || status === 'needs_review'

function extension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
}

async function readText(file: File): Promise<string> {
  try {
    return typeof file.text === 'function' ? await file.text() : ''
  } catch {
    return ''
  }
}

const unquote = (value: string) => value.trim().replace(/^(['"])(.*)\1$/, '$2')

/** Top-level keys of a JSON document or a simple YAML file (scalars, inline lists and "- item" lists). */
export function parseDocument(text: string, ext: string): Record<string, unknown> {
  if (ext === '.json') {
    try {
      const value: unknown = JSON.parse(text)
      return isRecord(value) ? value : {}
    } catch {
      return {}
    }
  }
  const out: Record<string, unknown> = {}
  let listKey: string | null = null
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+#.*$/, '')
    if (!line.trim() || line.trim().startsWith('#')) continue
    const item = /^\s*-\s+(.+)$/.exec(line)
    if (item && listKey) {
      ;(out[listKey] as string[]).push(unquote(item[1]))
      continue
    }
    const pair = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(line)
    if (!pair) {
      listKey = null
      continue
    }
    const [, key, value] = pair
    if (value.trim() === '') {
      out[key] = []
      listKey = key
      continue
    }
    listKey = null
    const inline = /^\[(.*)\]$/.exec(value.trim())
    out[key] = inline ? inline[1].split(',').map(unquote).filter(Boolean) : unquote(value)
  }
  return out
}

export type MockUploads = ReturnType<typeof createMockUploads>

export function createMockUploads(
  world: MockWorld,
  relayout: MockRelayout,
  hooks: { publish(rev: string): void; defaultParent(): string },
) {
  const jobs: JobState[] = []
  const profiles = new Map(MOCK_PROFILES.map((p) => [p.profile_id, { ...p }]))
  const mappedLayers = new Set<string>()
  let sequence = 0
  const tables: Record<string, { rows: number; updated_at: string }> = {
    brands: { rows: world.brands.size, updated_at: '2026-09-30T18:00:00+09:00' },
    tenancies: { rows: 126, updated_at: '2026-09-30T18:00:00+09:00' },
    leases: { rows: 38, updated_at: '2026-09-30T18:00:00+09:00' },
    pos_terminals: { rows: 77, updated_at: '2026-09-30T18:00:00+09:00' },
  }
  const documentsUpdated: Record<string, string> = {
    relayout_brief: '2026-10-20T09:00:00+09:00',
    drawing_profile: '2026-09-01T09:00:00+09:00',
  }

  const find = (jobId: string): JobState => {
    const state = jobs.find((j) => j.job.job_id === jobId)
    if (!state) throw new ApiError(404, `업로드 작업을 찾을 수 없습니다: ${jobId}`)
    return state
  }

  const touch = (state: JobState) => {
    state.job.updated_at = kstNow()
  }

  function question(floor: string): LayerReviewItem {
    return {
      floor,
      layer: 'A-AREA-17',
      semantic: 'unit',
      confidence: 0.55,
      alternatives: ['walkway', 'zone'],
      samples: [`${floor}-C05`, `${floor}-C06`],
      question: '닫힌 폴리라인 12개가 있는 레이어입니다. 매장 경계인가요, 보행 통로인가요?',
    }
  }

  function startNextStep(state: JobState): void {
    const next = state.job.steps.find((s) => s.status === 'pending')
    if (next) next.status = 'running'
    else finish(state)
  }

  function finish(state: JobState): void {
    const { job } = state
    job.status = job.parse?.review_queue.length ? 'needs_review' : 'done'
    if (job.rev_id) hooks.publish(job.rev_id)
  }

  function fail(state: JobState, message: string): void {
    const { job } = state
    for (const step of job.steps) if (step.status === 'pending') step.status = 'skipped'
    job.status = 'failed'
    job.error = message
  }

  /** Finish the running step with its result, then start the next one. */
  function finishStep(state: JobState): void {
    const { job } = state
    const step = job.steps.find((s) => s.status === 'running')
    if (!step || !job.rev_id || !job.parent_rev) return
    const rev = job.rev_id
    const parent = job.parent_rev
    switch (step.name) {
      case 'save':
        step.message = `파일 ${job.files.length}개 저장`
        break
      case 'parse': {
        if (state.broken) {
          step.status = 'failed'
          step.message = 'DXF 형식 오류'
          fail(state, `${state.broken}을(를) 읽지 못했습니다. DXF 형식이 올바른지 확인해 주세요.`)
          return
        }
        const parentFloors = world.revisions.get(parent)?.floors ?? {}
        const units = job.floors_uploaded.reduce((sum, f) => sum + (parentFloors[f]?.units.length ?? UNITS_PER_UNKNOWN_FLOOR), 0)
        const queue = state.questions.filter((q) => !mappedLayers.has(q.layer))
        job.parse = { units, issues: { error: 0, warning: queue.length + 2, info: 5 }, review_queue: queue }
        step.message = `매장 ${units}개${queue.length ? ` · 확인할 레이어 ${queue.length}개` : ''}`
        break
      }
      case 'merge': {
        const revision = buildUploadedRevision(world, rev, parent, job.effective_date ?? '', parent === SEASON_BASE_REV)
        if (!revision) {
          step.status = 'failed'
          fail(state, `부모 개정 ${parent}를 찾을 수 없습니다.`)
          return
        }
        world.revisions.set(rev, revision)
        step.message = `올린 층 ${job.floors_uploaded.join(', ')} · 가져온 층 ${job.floors_inherited.length}개`
        break
      }
      case 'diff': {
        const items = changesBetween(world, parent, rev)
        const byType: Record<string, number> = {}
        for (const item of items) byType[item.change_type] = (byType[item.change_type] ?? 0) + 1
        job.changes = { total: items.length, by_type: byType }
        step.message = `변경 ${items.length}건`
        break
      }
      case 'qa': {
        const units = world.revisions.get(rev)?.floors['3F']?.units ?? []
        const c06 = units.find((u) => u.code === '3F-C06')
        const seasonDrawing = parent === SEASON_BASE_REV && !!c06
        const issues = seasonDrawing
          ? [
              {
                severity: 'warning',
                code: 'area_mismatch',
                unit_code: '3F-C06',
                message: `계약 면적 456.0㎡와 도면 면적 ${rectArea(c06.rect).toFixed(1)}㎡가 ${(((456 - rectArea(c06.rect)) / 456) * 100).toFixed(1)}% 다릅니다.`,
              },
              { severity: 'warning', code: 'brand_mismatch', unit_code: '3F-N02', message: '도면 브랜드 Talvessa와 계약 브랜드 Dovrelle이 다릅니다.' },
            ]
          : []
        // Occupied units whose (mock) lease ended before the effective date: informational ("참고") rows.
        const effective = job.effective_date ?? ''
        const infos = seasonDrawing
          ? units
              .filter((u) => u.brandId && u.code !== '3F-C06' && u.code !== '3F-N02' && mockLeaseEnd(u.brandId) < effective)
              .sort((a, b) => a.code.localeCompare(b.code))
              .map((u) => ({
                severity: 'info',
                code: 'unit_without_lease',
                unit_code: u.code,
                message: `${u.code} ${world.brands.get(u.brandId!)?.name ?? u.brandId}: 적용일(${effective})에 유효한 계약이 없습니다. 신규·갱신 계약을 등록해야 합니다.`,
              }))
          : []
        job.qa = { errors: 0, warnings: issues.length, infos: infos.length, issues: [...issues, ...infos] }
        step.message = `오류 0건 · 경고 ${issues.length}건 · 참고 ${infos.length}건`
        break
      }
      case 'impact': {
        const impact = planImpact(changesBetween(world, parent, rev))
        job.impact = impact.map((i) => ({ generator: i.generator, title_ko: i.title_ko, risk_tier: i.risk_tier }))
        step.message = `다시 만들 산출물 ${impact.length}종`
        break
      }
      case 'generate': {
        if (!state.runGenerators) {
          step.status = 'skipped'
          step.message = '산출물 재생성을 끄고 올렸습니다.'
          break
        }
        const impact = planImpact(changesBetween(world, parent, rev))
        job.artifacts = impact.map((i) => ({ generator: i.generator, status: i.approval_mode === 'pre_publish' ? 'awaiting_approval' : 'published' }))
        step.message = `산출물 ${impact.length}종 생성`
        break
      }
      case 'relayout_check': {
        const check = relayout.checkUpload(rev, parent)
        if (!check) {
          step.status = 'skipped'
          step.message = `${parent} 기준으로 결정한 개편안이 없습니다.`
          break
        }
        job.relayout_check = check
        step.message = `${check.brief_id} ${check.alt_id ?? ''} 일치율 ${check.conformance_pct ?? '–'}%`
        break
      }
    }
    if (step.status === 'running') step.status = 'done'
    startNextStep(state)
  }

  /** One poll: start the oldest waiting job or finish up to two of its steps. */
  function advance(): void {
    const head = jobs.find((j) => !settled(j.job.status))
    if (!head) return
    for (let action = 0; action < ACTIONS_PER_POLL && !settled(head.job.status); action += 1) {
      if (head.job.status === 'queued') {
        head.job.status = 'running'
        startNextStep(head)
      } else {
        finishStep(head)
      }
    }
    touch(head)
  }

  const summary = ({ job }: JobState): UploadJobSummary => ({
    job_id: job.job_id,
    kind: job.kind,
    status: job.status,
    rev_id: job.rev_id,
    created_at: job.created_at,
    updated_at: job.updated_at,
  })

  return {
    uploadDrawings(request: DrawingUploadRequest): UploadJobCreated {
      const revId = (request.rev_id ?? '').trim()
      const existing = world.revisions.get(revId)
      if (existing && existing.modelSource !== 'uploaded') {
        throw new ApiError(409, `${revId}는 이미 있는 개정입니다. 업로드로 만든 개정만 같은 ID로 다시 올릴 수 있습니다.`)
      }
      const files = request.files ?? []
      const errors = validateDrawingUpload({ files, revId, effectiveDate: request.effective_date ?? '' })
      if (errors.length) throw new ApiError(400, errors.map((e) => e.message).join(' '))
      const parent = request.parent_rev || hooks.defaultParent()
      const parentRevision = world.revisions.get(parent)
      if (!parentRevision) throw new ApiError(400, `알 수 없는 부모 개정입니다: ${parent}`)
      if (parentRevision.info.kind === 'hypothetical') throw new ApiError(400, '부모 개정은 실제 개정이어야 합니다.')
      const profile = request.profile || 'default'
      if (!profiles.has(profile)) throw new ApiError(400, `알 수 없는 도면 규약 프로필입니다: ${profile}`)
      if (request.units && request.units !== 'm' && request.units !== 'mm') throw new ApiError(400, '단위는 m 또는 mm여야 합니다.')

      const zip = files.find((f) => extension(f.name) === '.zip')
      const fileFloors = files.map((f) => ({ name: f.name, floor: zip ? null : floorFromFileName(f.name), bytes: f.size }))
      const uploaded = zip ? [floorFromFileName(zip.name) ?? '3F'] : [...new Set(fileFloors.map((f) => f.floor!))]
      const now = kstNow()
      sequence += 1
      const jobId = `J${now.slice(0, 10).replace(/-/g, '')}-${String(sequence).padStart(4, '0')}`
      const job: UploadJob = {
        job_id: jobId,
        kind: 'drawing',
        status: 'queued',
        rev_id: revId,
        created_at: now,
        updated_at: now,
        parent_rev: parent,
        effective_date: request.effective_date,
        profile,
        files: fileFloors,
        floors_uploaded: uploaded,
        floors_inherited: MOCK_FLOORS.map((f) => f.code as string).filter((code) => !uploaded.includes(code)),
        steps: STEP_ORDER.map((name) => ({ name, label: STEP_LABELS[name], status: 'pending', message: null })),
        parse: null,
        changes: null,
        impact: [],
        artifacts: [],
        qa: null,
        relayout_check: null,
        error: null,
      }
      jobs.push({
        job,
        questions: profile === 'default' ? [question(uploaded[0])] : [],
        broken: files.find((f) => /broken/i.test(f.name))?.name ?? null,
        runGenerators: request.run_generators !== false,
      })
      return { job_id: jobId, status: 'queued' }
    },

    listJobs(): UploadJobSummary[] {
      return [...jobs].reverse().slice(0, 20).map(summary)
    },

    /** GET of one job; every call is one poll that moves the job queue forward. */
    getJob(jobId: string): UploadJob {
      const state = find(jobId)
      advance()
      return structuredClone(state.job)
    },

    submitLayerMapping(jobId: string, mappings: LayerMapping[]): UploadJob {
      const state = find(jobId)
      if (!settled(state.job.status)) throw new ApiError(409, '처리 중인 작업은 레이어 매핑을 바꿀 수 없습니다. 끝난 뒤 다시 시도해 주세요.')
      if (!Array.isArray(mappings) || mappings.length === 0) throw new ApiError(400, '저장할 레이어 매핑이 없습니다.')
      for (const m of mappings) {
        if (!m.layer) throw new ApiError(400, '레이어 이름이 없는 매핑이 있습니다.')
        if (!LAYER_SEMANTICS.some((s) => s.value === m.semantic)) throw new ApiError(400, `알 수 없는 레이어 의미입니다: ${m.semantic}`)
      }
      for (const m of mappings) mappedLayers.add(m.layer)
      const { job } = state
      // Keep the saved files; parse again with the new mapping.
      for (const step of job.steps) {
        if (step.name !== 'save') {
          step.status = 'pending'
          step.message = null
        }
      }
      Object.assign(job, { status: 'queued', parse: null, changes: null, impact: [], artifacts: [], qa: null, relayout_check: null, error: null })
      touch(state)
      return structuredClone(job)
    },

    listProfiles: (): DrawingProfile[] => [...profiles.values()].map((p) => ({ ...p })),

    async uploadMetadata(kind: MetadataKind, file: File): Promise<MetadataUploadResult> {
      const info = metadataKindInfo(kind)
      if (!info) throw new ApiError(400, `알 수 없는 메타데이터 종류입니다: ${kind}`)
      const result: MetadataUploadResult = { kind, status: 'rejected', rows: null, columns: [], errors: [], warnings: [], backup: null, id: null }
      const ext = extension(file.name)
      if (!info.extensions.includes(ext)) return { ...result, errors: [`${info.label} 종류는 ${info.formats} 파일만 받습니다: ${file.name}`] }
      if (/bad/i.test(file.name)) {
        const table = info.extensions.includes('.csv')
        return {
          ...result,
          errors: table
            ? [`필수 열이 없습니다: ${info.required[1]}`, `3행: ${info.required[0]} 값이 비어 있습니다.`]
            : [`필수 키가 없습니다: ${info.required[1]}`],
        }
      }
      const content = await readText(file)
      const now = kstNow()
      const stamp = now.slice(0, 19).replace(/[-:]/g, '')

      if (kind !== 'relayout_brief' && kind !== 'drawing_profile') {
        const lines = content.split(/\r?\n/).filter((l) => l.trim())
        const header = ext === '.csv' && lines.length ? lines[0].split(',').map(unquote) : [...info.required, ...info.optional]
        const rows = ext === '.csv' ? Math.max(0, lines.length - 1) : tables[kind].rows
        const warnings = [
          ...(kind === 'leases' && !header.includes('commission_rate') ? ['commission_rate 열이 없어 기본 수수료율(27%)을 씁니다.'] : []),
          ...(rows === 0 ? ['데이터 행이 없습니다.'] : []),
        ]
        const backup = `metadata/backup/${kind}_${stamp}${ext}`
        tables[kind] = { rows, updated_at: now }
        return { ...result, status: 'accepted', rows, columns: header, warnings, backup }
      }

      const fields = parseDocument(content, ext)
      const idKey = kind === 'relayout_brief' ? 'brief_id' : 'profile_id'
      const base = file.name.replace(/\.[^.]*$/, '')
      const id = typeof fields[idKey] === 'string' && fields[idKey] ? String(fields[idKey]) : kind === 'relayout_brief' ? base.toUpperCase() : base
      const missing = info.required.filter((key) => fields[key] === undefined || fields[key] === '')
      const warnings = missing.length ? [`필수 키가 없어 예시 값을 씁니다(모의 모드): ${missing.join(', ')}`] : []
      const replacing = kind === 'relayout_brief' ? relayout.briefIds().includes(id) : profiles.has(id)
      const backup = replacing ? `metadata/backup/${kind}_${id}_${stamp}${ext}` : null
      const str = (key: string) => (typeof fields[key] === 'string' && fields[key] ? String(fields[key]) : undefined)
      if (kind === 'relayout_brief') {
        const floors = Array.isArray(fields.floors) ? fields.floors.map(String) : undefined
        const year = Number(fields.year)
        relayout.putBrief({
          brief_id: id,
          title: str('title') ?? id,
          season: str('season')?.toUpperCase() ?? 'FW',
          ...(Number.isFinite(year) && year > 2000 ? { year } : {}),
          ...(str('effective_date') ? { effective_date: str('effective_date') } : {}),
          ...(str('base_rev') && world.revisions.has(str('base_rev')!) ? { base_rev: str('base_rev') } : {}),
          ...(floors?.length ? { floors } : {}),
        })
      } else {
        profiles.set(id, {
          profile_id: id,
          title: str('title') ?? id,
          description: str('description') ?? '업로드한 도면 규약 프로필',
          units: str('units') === 'mm' ? 'mm' : 'm',
        })
      }
      documentsUpdated[kind] = now
      return { ...result, status: 'accepted', rows: 1, columns: Object.keys(fields), warnings, backup, id }
    },

    metadataStatus(): MetadataStatus[] {
      return METADATA_KINDS.map((info) => {
        const items = info.kind === 'relayout_brief' ? relayout.briefIds() : info.kind === 'drawing_profile' ? [...profiles.keys()] : []
        const table = tables[info.kind]
        return {
          kind: info.kind,
          present: table ? table.rows > 0 : items.length > 0,
          rows: table ? table.rows : items.length,
          updated_at: table ? table.updated_at : (documentsUpdated[info.kind] ?? null),
          required_columns: [...info.required],
          items,
        }
      })
    },
  }
}
