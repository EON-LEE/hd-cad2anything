import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { ApiError, normalizeMeta, type AtlasDataSource } from '../api/client'
import { defaultExpandedAlternative } from '../lib/relayout'
import { pickDefaultRevision } from '../state/uiState'
import type { MetadataKind, UploadJob } from '../types/api'
import { setMockSpeed } from './chatScripts'
import { createMockSource } from './mockSource'
import { parseDocument } from './uploads'

beforeAll(() => setMockSpeed(0))
afterAll(() => setMockSpeed(1))

const dxf = (name: string) => new File(['0\nSECTION\n'], name)

async function pollUntilSettled(source: AtlasDataSource, jobId: string): Promise<{ job: UploadJob; seen: string[] }> {
  const seen: string[] = []
  for (let i = 0; i < 20; i += 1) {
    const job = await source.getUploadJob(jobId)
    seen.push(job.status)
    if (['done', 'failed', 'needs_review'].includes(job.status)) return { job, seen }
  }
  throw new Error(`job ${jobId} did not settle`)
}

describe('mock seasonal relayout', () => {
  it('runs the FW2026 brief from diagnosis to a decision', async () => {
    const source = createMockSource()
    expect(await source.listRelayoutBriefs()).toEqual([
      expect.objectContaining({
        brief_id: 'FW2026',
        title: '2026 F/W MD 개편',
        season: 'FW',
        year: 2026,
        effective_date: '2026-10-01',
        base_rev: 'R2',
        floors: ['4F', '5F', '6F'],
        objective: 'commission',
        status: 'draft',
        entries: 2,
        exits: 2,
      }),
    ])
    const project = await source.getRelayoutProject('FW2026')
    expect(project).toMatchObject({ status: 'draft', diagnosis: null, plan: null, selected_alt_id: null, check: null })
    expect(project.brief).toMatchObject({ season_label: 'F/W(9~2월)', objective_label: '수수료 수익', area_tolerance: 0.15 })
    // Node can make blob URLs, so the mock report opens like the backend's HTML report.
    expect(project.report_url).toMatch(/^blob:/)
    await expect(source.getRelayoutProject('NOPE')).rejects.toMatchObject({ status: 404 })

    const diagnosis = await source.diagnoseRelayout('FW2026')
    expect(diagnosis.season).toMatchObject({ code: 'FW', last: { start: '2025-10-01', end: '2026-02-28' }, previous: { start: '2024-10-01', end: '2025-02-28' } })
    expect(diagnosis.candidates.find((c) => c.unit_code === '3F-E01')).toMatchObject({ brand_name: 'Quorelle', low_efficiency: true, reasons: ['계약 만료', '저효율'] })
    expect(diagnosis.candidate_total).toBe(diagnosis.candidates.length)
    expect(diagnosis.entries.map((e) => [e.brand_name, e.feasible_unit_codes, e.season_factor])).toEqual([
      ['Norvane Outdoor', ['3F-E01', '3F-E02'], 1.32],
      ['Velkira Knit', [], 1.02],
    ])
    expect(diagnosis.notes[0]).toContain('3층')
    expect((await source.listRelayoutBriefs())[0].status).toBe('diagnosed')

    const plan = await source.planRelayout('FW2026', { objective: 'commission', max_moves: 6, downtime_weeks: 2, movable: 'expiring' })
    expect(plan.alternatives.map((a) => `${a.alt_id}:${a.rev_id}:${a.kpi.moves}`)).toEqual([
      'ALT-0:FW2026-A0:0',
      'ALT-1:FW2026-A1:1',
      'ALT-2:FW2026-A2:1',
      'ALT-3:FW2026-A3:2',
    ])
    const [baseline, alt1, alt2, alt3] = plan.alternatives
    expect(baseline.kpi).toMatchObject({ sales_delta_krw: 0, objective_delta_krw: 0, move_cost_krw: 0, net_krw: 0 })
    expect(alt1.kpi.net_krw).toBe(alt1.kpi.objective_delta_krw! - alt1.kpi.move_cost_krw!)
    expect(alt1.kpi.vs_current_sales_krw).toBe(baseline.kpi.vs_current_sales_krw! + alt1.kpi.sales_delta_krw!)
    // ALT-1 has the best net effect, so the UI opens its card first.
    expect(alt1.kpi.net_krw!).toBeGreaterThan(Math.max(alt2.kpi.net_krw!, alt3.kpi.net_krw!))
    expect(defaultExpandedAlternative(plan, null)).toBe('ALT-1')
    expect(alt1.changes.map((c) => `${c.type}:${c.brand_name}:${c.from_unit ?? ''}>${c.to_unit ?? ''}`)).toEqual([
      'exit:Quorelle:3F-E01>',
      'exit:Ilvora Leather:3F-E02>',
      'move:Lumivane:3F-C06>3F-E01',
      'entry:Norvane Outdoor:>3F-C06',
    ])
    // Per-change explanations name the brand and unit; the card notes are the entry shift and the spillover.
    expect(alt1.changes.map((c) => c.explanation)).toEqual([
      expect.stringMatching(/^Quorelle\(3F-E01\) 퇴점: 계약 만료·저효율\. 지금 기대 매출 [\d.]+억 원\/년이 빠집니다\.$/),
      expect.stringMatching(/^Ilvora Leather\(3F-E02\) 퇴점: 계약 만료\./),
      expect.stringMatching(/^Lumivane\(.+\) 3F-C06→3F-E01: /),
      'Norvane Outdoor(3F-C06) 입점: 기대 매출 8.2억 원/년, 시즌 지수 1.32, 수수료율 26.0%.',
    ])
    expect(baseline.explanations).toEqual([])
    expect(alt1.explanations).toEqual([expect.stringContaining('신규 입점 Norvane Outdoor 위치 3F-E01→3F-C06'), expect.stringContaining('파급 효과')])
    expect(alt3.constraints.filter((c) => !c.ok).map((c) => c.name)).toEqual(['중앙 섬 공실 없음'])
    expect(plan.unplaced).toEqual([expect.objectContaining({ brand_name: 'Velkira Knit', reason: expect.stringContaining('120~180㎡') })])

    // Each alternative is a hypothetical revision drawn on R2, listed in /api/meta after planning.
    const meta = normalizeMeta(await source.getMeta())
    expect(meta.revisions.filter((r) => r.rev_id.startsWith('FW2026-')).map((r) => `${r.rev_id}:${r.kind}:${r.parent_rev_id}:${r.model_source}`)).toEqual([
      'FW2026-A0:hypothetical:R2:hypothetical',
      'FW2026-A1:hypothetical:R2:hypothetical',
      'FW2026-A2:hypothetical:R2:hypothetical',
      'FW2026-A3:hypothetical:R2:hypothetical',
    ])
    const floor = await source.getFloor('FW2026-A1', '3F', false)
    const unit = (code: string) => floor.features.find((f) => f.properties.unit_code === code)?.properties
    expect(unit('3F-C06')?.brand_name).toBe('Norvane Outdoor')
    expect(unit('3F-E01')?.brand_name).toBe('Lumivane')
    expect(unit('3F-E02')?.vacant).toBe(true)
    expect((await source.getChanges('R2', 'FW2026-A1')).change_set.items).toHaveLength(4)

    // Options change the numbers; fewer allowed moves drop ALT-3.
    const bySales = await source.planRelayout('FW2026', { objective: 'sales', max_moves: 1, downtime_weeks: 4 })
    expect(bySales.alternatives.map((a) => a.alt_id)).toEqual(['ALT-0', 'ALT-1', 'ALT-2'])
    expect(bySales.params).toMatchObject({ objective: 'sales', max_moves: 1, downtime_weeks: 4, movable: 'expiring' })
    expect(bySales.alternatives[1].kpi.objective_delta_krw!).toBeGreaterThan(alt1.kpi.objective_delta_krw!)
    expect(bySales.alternatives[1].kpi.move_cost_krw!).toBeGreaterThan(alt1.kpi.move_cost_krw!)

    await expect(source.checkRelayout('FW2026', 'R1')).rejects.toMatchObject({ status: 400 })
    await expect(source.selectRelayoutAlternative('FW2026', { alt_id: 'ALT-9', decided_by: 'MD팀' })).rejects.toMatchObject({ status: 400 })
    const selected = await source.selectRelayoutAlternative('FW2026', { alt_id: 'ALT-1', decided_by: 'MD팀 김OO' })
    expect(selected).toMatchObject({ status: 'selected', selected_alt_id: 'ALT-1', decided_by: 'MD팀 김OO', check: null })
    expect(selected.decided_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$/)
    await expect(source.checkRelayout('FW2026', 'FW2026-A1')).rejects.toMatchObject({ status: 400 })
    await expect(source.checkRelayout('FW2026', 'R9')).rejects.toMatchObject({ status: 404 })
  })
})

describe('mock uploads', () => {
  it('advances a drawing job over a few polls, asks about one layer and finishes after the mapping', async () => {
    const source = createMockSource()
    const created = await source.uploadDrawings({ files: [dxf('R3_3F.dxf')], rev_id: 'R3', effective_date: '2026-10-01' })
    expect(created).toEqual({ job_id: expect.stringMatching(/^J\d{8}-0001$/), status: 'queued' })
    expect((await source.listUploadJobs())[0]).toMatchObject({ job_id: created.job_id, kind: 'drawing', status: 'queued', rev_id: 'R3' })

    const first = await source.getUploadJob(created.job_id)
    expect(first.status).toBe('running')
    expect(first.steps.map((s) => s.status)).toEqual(['done', 'running', 'pending', 'pending', 'pending', 'pending', 'pending', 'pending'])
    expect(first.steps.map((s) => s.label)).toEqual(['파일 저장', '도면 읽기', '층 합치기', '변경 비교', '계약 정보 대조', '영향 분석', '산출물 재생성', '개편안 대조'])

    const { job, seen } = await pollUntilSettled(source, created.job_id)
    expect(seen).toEqual(['running', 'running', 'running', 'needs_review'])
    expect(job).toMatchObject({ parent_rev: 'R2', profile: 'default', floors_uploaded: ['3F'], floors_inherited: ['1F'], error: null })
    expect(job.steps.map((s) => s.status)).toEqual(['done', 'done', 'done', 'done', 'done', 'done', 'done', 'skipped'])
    expect(job.parse?.review_queue).toEqual([expect.objectContaining({ floor: '3F', layer: 'A-AREA-17', semantic: 'unit', confidence: 0.55 })])
    expect(job.changes).toEqual({ total: 6, by_type: { reassign: 4, move: 1, resize: 1 } })
    expect(job.qa).toMatchObject({ errors: 0, warnings: 2, infos: 2 })
    expect(job.qa?.issues.map((i) => `${i.severity}:${i.code}:${i.unit_code}`)).toEqual([
      'warning:area_mismatch:3F-C06',
      'warning:brand_mismatch:3F-N02',
      // Studio Velune stays and Lumivane moved in; both leases ended on 2026-09-30.
      'info:unit_without_lease:3F-C01',
      'info:unit_without_lease:3F-E02',
    ])
    expect(job.qa?.issues[3].message).toBe('3F-E02 Lumivane: 적용일(2026-10-01)에 유효한 계약이 없습니다. 신규·갱신 계약을 등록해야 합니다.')
    expect(job.artifacts.length).toBeGreaterThan(0)

    // The model is made even when layers need review; it is listed without sales (after data_through).
    const meta = normalizeMeta(await source.getMeta())
    expect(meta.revisions.find((r) => r.rev_id === 'R3')).toMatchObject({ kind: 'actual', parent_rev_id: 'R2', has_sales: false, model_source: 'uploaded' })
    expect(pickDefaultRevision(meta)).toBe('R2')
    expect((await source.getArtifacts('R3')).length).toBeGreaterThan(0)

    await expect(source.submitLayerMapping(created.job_id, [{ layer: 'A-AREA-17', semantic: 'hallway' }])).rejects.toMatchObject({ status: 400 })
    const resumed = await source.submitLayerMapping(created.job_id, [{ layer: 'A-AREA-17', semantic: 'walkway', floor: '3F' }])
    expect(resumed.status).toBe('queued')
    expect(resumed.steps.map((s) => s.status)).toEqual(['done', 'pending', 'pending', 'pending', 'pending', 'pending', 'pending', 'pending'])
    await expect(source.submitLayerMapping(created.job_id, [{ layer: 'A-AREA-17', semantic: 'walkway' }])).rejects.toMatchObject({ status: 409 })
    const again = await pollUntilSettled(source, created.job_id)
    expect(again.job.status).toBe('done')
    expect(again.job.parse?.review_queue).toEqual([])

    // The saved mapping is used for the next upload.
    const next = await source.uploadDrawings({ files: [dxf('3F.dxf')], rev_id: 'R3', effective_date: '2026-10-01' })
    expect((await pollUntilSettled(source, next.job_id)).job.status).toBe('done')
  })

  it('checks an upload against the decided relayout alternative of its parent', async () => {
    const source = createMockSource()
    await source.planRelayout('FW2026', {})
    await source.selectRelayoutAlternative('FW2026', { alt_id: 'ALT-1', decided_by: 'MD팀' })
    const created = await source.uploadDrawings({
      files: [dxf('R3_3F.dxf')],
      rev_id: 'R3',
      parent_rev: 'R2',
      effective_date: '2026-10-01',
      profile: 'customer_style',
      run_generators: false,
    })
    const { job } = await pollUntilSettled(source, created.job_id)
    expect(job.status).toBe('done')
    expect(job.steps.find((s) => s.name === 'generate')?.status).toBe('skipped')
    expect(job.artifacts).toEqual([])
    expect(job.relayout_check).toMatchObject({ brief_id: 'FW2026', alt_id: 'ALT-1', conformance_pct: 75 })

    const project = await source.getRelayoutProject('FW2026')
    expect(project.status).toBe('checked')
    expect(project.check?.items.map((i) => `${i.type}:${i.status}:${i.status_label}`)).toEqual([
      'exit:match:일치',
      'exit:match:일치',
      'move:different_unit:다른 매장',
      'entry:match:일치',
    ])
    expect(project.check?.items[2]).toMatchObject({ type_label: '이전', from_unit: '3F-C06', planned_unit: '3F-E01', actual_unit: '3F-E02' })
    expect(project.check?.unexpected).toEqual([
      { unit_code: '3F-N02', before_brand: 'BR3006', before_brand_name: 'Dovrelle', after_brand: 'BR3007', after_brand_name: 'Talvessa' },
    ])
    expect(project.check?.area_changes).toEqual([{ unit_code: '3F-C06', before_m2: 456, after_m2: 437, delta_pct: -4.2 }])
    expect((await source.checkRelayout('FW2026', 'R3')).conformance_pct).toBe(75)

    // Deciding ALT-2 instead: the entry and the move were drawn elsewhere, Lumivane's move is unplanned.
    await source.selectRelayoutAlternative('FW2026', { alt_id: 'ALT-2', decided_by: 'MD팀' })
    const alt2 = await source.checkRelayout('FW2026', 'R3')
    expect(alt2.conformance_pct).toBe(50)
    expect(alt2.items.filter((i) => i.status === 'different_unit').map((i) => `${i.brand_name}:${i.planned_unit}>${i.actual_unit}`)).toEqual([
      'Studio Velune:3F-E02>3F-C01',
      'Norvane Outdoor:3F-C01>3F-C06',
    ])
    expect(alt2.unexpected.map((u) => `${u.unit_code}:${u.before_brand_name}>${u.after_brand_name}`).sort()).toEqual([
      '3F-C06:Lumivane>Norvane Outdoor',
      '3F-N02:Dovrelle>Talvessa',
    ])
  })

  it('runs one job at a time and rejects invalid uploads like the backend', async () => {
    const source = createMockSource()
    const request = { rev_id: 'R3', effective_date: '2026-10-01' }
    await expect(source.uploadDrawings({ ...request, files: [dxf('R2_3F.dxf')], rev_id: 'R2' })).rejects.toMatchObject({ status: 409 })
    await expect(source.uploadDrawings({ ...request, files: [dxf('plan.dxf')] })).rejects.toMatchObject({ status: 400, message: expect.stringContaining('층 코드') })
    await expect(source.uploadDrawings({ ...request, files: [dxf('3F.dxf'), dxf('R3_3F.dxf')] })).rejects.toMatchObject({ status: 400 })
    await expect(source.uploadDrawings({ ...request, files: [dxf('3F.dxf')], profile: 'nope' })).rejects.toMatchObject({ status: 400 })
    await expect(source.uploadDrawings({ ...request, files: [dxf('3F.dxf')], parent_rev: 'H1' })).rejects.toMatchObject({ status: 400 })

    const a = await source.uploadDrawings({ ...request, files: [dxf('R3_3F.dxf')] })
    const b = await source.uploadDrawings({ ...request, rev_id: 'R4', files: [dxf('R4_1F_broken.dxf')] })
    // Polling the second job moves the queue, but it waits for the first one.
    expect((await source.getUploadJob(b.job_id)).status).toBe('queued')
    expect((await source.getUploadJob(a.job_id)).status).toBe('running')
    const { job } = await pollUntilSettled(source, b.job_id)
    expect((await source.getUploadJob(a.job_id)).status).toBe('needs_review')
    expect(job.status).toBe('failed')
    expect(job.error).toContain('R4_1F_broken.dxf')
    expect(job.steps.map((s) => s.status)).toEqual(['done', 'failed', 'skipped', 'skipped', 'skipped', 'skipped', 'skipped', 'skipped'])
    expect(normalizeMeta(await source.getMeta()).revisions.some((r) => r.rev_id === 'R4')).toBe(false)
    expect((await source.listUploadJobs()).map((j) => `${j.rev_id}:${j.status}`)).toEqual(['R4:failed', 'R3:needs_review'])
    await expect(source.getUploadJob('J-missing')).rejects.toBeInstanceOf(ApiError)
    // An uploaded revision may be uploaded again.
    await expect(source.uploadDrawings({ ...request, files: [dxf('3F.dxf')] })).resolves.toMatchObject({ status: 'queued' })
  })

  it('accepts metadata files, rejects files named "bad" and reports the status', async () => {
    const source = createMockSource()
    const brands = await source.uploadMetadata('brands', new File(['brand_id,brand_name,category\nB1,Alpha,LUXURY\nB2,Beta,CAFE\n'], 'brands.csv'))
    expect(brands).toMatchObject({
      kind: 'brands',
      status: 'accepted',
      rows: 2,
      columns: ['brand_id', 'brand_name', 'category'],
      errors: [],
      warnings: [],
      backup: expect.stringMatching(/^metadata\/backup\/brands_\d{8}T\d{6}\.csv$/),
    })
    const bad = await source.uploadMetadata('leases', new File(['x'], 'bad_leases.csv'))
    expect(bad).toMatchObject({ status: 'rejected', errors: ['필수 열이 없습니다: brand_id', '3행: lease_id 값이 비어 있습니다.'], backup: null })
    expect((await source.uploadMetadata('brands', new File(['x'], 'brands.yaml'))).errors[0]).toContain('CSV·XLSX')
    const leases = await source.uploadMetadata('leases', new File(['lease_id,brand_id,unit_code,lease_start,lease_end\nL1,B1,3F-C01,2025-01-01,2026-12-31\n'], 'leases.csv'))
    expect(leases.warnings).toEqual(['commission_rate 열이 없어 기본 수수료율(27%)을 씁니다.'])

    const brief = await source.uploadMetadata(
      'relayout_brief',
      new File(['brief_id: SS2027\ntitle: 2027 S/S 개편\nseason: SS\nyear: 2027\neffective_date: 2027-03-01\nbase_rev: R2\nfloors: [1F, 3F]\n'], 'ss2027.yaml'),
    )
    expect(brief).toMatchObject({ status: 'accepted', id: 'SS2027', rows: 1, warnings: [], backup: null })
    expect((await source.listRelayoutBriefs()).map((b) => `${b.brief_id}:${b.status}`)).toEqual(['FW2026:draft', 'SS2027:draft'])
    expect((await source.getRelayoutProject('SS2027')).brief).toMatchObject({ title: '2027 S/S 개편', season: 'SS', year: 2027, floors: ['1F', '3F'] })

    const profile = await source.uploadMetadata('drawing_profile', new File(['{"profile_id": "vendor_b", "title": "협력사 B", "units": "mm"}'], 'vendor_b.json'))
    expect(profile).toMatchObject({ status: 'accepted', id: 'vendor_b' })
    expect((await source.listDrawingProfiles()).map((p) => p.profile_id)).toEqual(['default', 'customer_style', 'vendor_b'])

    const status = await source.getMetadataStatus()
    expect(status.map((s) => s.kind)).toEqual(['brands', 'tenancies', 'leases', 'pos_terminals', 'relayout_brief', 'drawing_profile'])
    expect(status[0]).toMatchObject({ present: true, rows: 2, items: [], required_columns: ['brand_id', 'brand_name', 'category'] })
    expect(status[4].items).toEqual(['FW2026', 'SS2027'])
    expect(status[5].items).toEqual(['default', 'customer_style', 'vendor_b'])
    await expect(source.uploadMetadata('nope' as MetadataKind, new File(['x'], 'x.csv'))).rejects.toMatchObject({ status: 400 })
    expect(parseDocument('floors:\n- 4F\n  - "5F"\n# 주석\nyear: 2026 # 끝', '.yaml')).toEqual({ floors: ['4F', '5F'], year: '2026' })
  })
})
