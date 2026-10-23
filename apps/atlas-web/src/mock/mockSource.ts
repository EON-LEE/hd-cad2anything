// In-browser implementation of the Atlas API backed by the synthetic mock world.
import { ApiError, type AtlasDataSource } from '../api/client'
import { pickDefaultRevision } from '../state/uiState'
import type { Artifact, ChatRequest } from '../types/api'
import { buildMockArtifacts, mockLayoutAlternatives, mockProductivity, planImpact } from './analytics'
import { buildMockScript, playScript, sleep } from './chatScripts'
import { buildMockFloor, buildMockMeta } from './floors'
import { mockJourney } from './journey'
import { createMockRelayout } from './relayout'
import { createMockUploads } from './uploads'
import { changesBetween, getWorld, type MockWorld } from './world'

const clone = <T>(value: T): T => structuredClone(value)

export function createMockSource(): AtlasDataSource {
  const shared = getWorld()
  // Revisions made by relayout plans and uploads stay in this source (tests create several sources).
  const world: MockWorld = { brands: shared.brands, revisions: new Map(shared.revisions), order: [...shared.order] }
  const artifacts = new Map<string, Artifact[]>()
  let runCounter = 0

  /** Make a revision visible in /api/meta (and drop artifacts cached for an earlier upload of it). */
  const publish = (rev: string) => {
    if (!world.order.includes(rev)) world.order.push(rev)
    artifacts.delete(rev)
  }
  const relayout = createMockRelayout(world, publish)
  const uploads = createMockUploads(world, relayout, { publish, defaultParent: () => pickDefaultRevision(buildMockMeta(world)) })

  const artifactsFor = (rev: string): Artifact[] => {
    let list = artifacts.get(rev)
    if (!list) {
      const built = buildMockArtifacts(world, rev)
      if (!built) throw new ApiError(404, `알 수 없는 개정: ${rev}`)
      list = built
      artifacts.set(rev, list)
    }
    return list
  }

  const requireRevision = (rev: string) => {
    if (!world.revisions.has(rev)) throw new ApiError(404, `알 수 없는 개정: ${rev}`)
  }

  return {
    mode: 'mock',
    async getMeta() {
      await sleep(40)
      return clone(buildMockMeta(world))
    },
    async getFloor(rev, floor, includeFixtures) {
      await sleep(80)
      const fc = buildMockFloor(world, rev, floor, includeFixtures)
      if (!fc) throw new ApiError(404, `${rev} ${floor} 도면이 없습니다 (모의 데이터는 1F·3F만 제공).`)
      return fc
    },
    async getChanges(revFrom, revTo) {
      await sleep(80)
      requireRevision(revFrom)
      requireRevision(revTo)
      const items = changesBetween(world, revFrom, revTo)
      return clone({ change_set: { rev_from: revFrom, rev_to: revTo, items }, impact: planImpact(items) })
    },
    async getArtifacts(rev) {
      await sleep(80)
      return clone(artifactsFor(rev))
    },
    async reviewArtifact(rev, generator, body) {
      await sleep(150)
      const list = artifactsFor(rev)
      const index = list.findIndex((a) => a.generator === generator)
      if (index < 0) throw new ApiError(404, `산출물이 없습니다: ${rev}/${generator}`)
      const current = list[index]
      if (current.status !== 'awaiting_approval') {
        throw new ApiError(409, `${current.title_ko}은(는) ${current.status} 상태라 검토할 수 없습니다.`)
      }
      const updated: Artifact = {
        ...current,
        status: body.decision === 'approve' ? 'published' : 'rejected',
        reviewed_by: body.reviewer,
        review_note: body.note,
      }
      list[index] = updated
      return clone(updated)
    },
    async getProductivity(rev, floor) {
      await sleep(80)
      const result = mockProductivity(world, rev, floor)
      if (!result) throw new ApiError(404, `${rev} ${floor} 생산성 데이터가 없습니다.`)
      return result
    },
    async getLayoutAlternatives(body) {
      await sleep(400)
      requireRevision(body.rev)
      return { alternatives: mockLayoutAlternatives(world, body) }
    },
    async getJourney(params) {
      await sleep(250)
      const journey = mockJourney(world, params)
      if (!journey) throw new ApiError(404, '조건에 맞는 고객 동선이 없습니다.')
      return clone(journey)
    },
    async streamChat(body: ChatRequest, onEvent, signal) {
      runCounter += 1
      const threadId = body.thread_id || `mock-thread-${Date.now().toString(36)}`
      const runId = `mock-run-${runCounter}`
      await playScript(buildMockScript(world, body, threadId, runId), onEvent, signal)
    },
    artifactFileUrl(_rev, _generator, file) {
      return file.url ?? null
    },

    async listRelayoutBriefs() {
      await sleep(60)
      return relayout.listBriefs()
    },
    async getRelayoutProject(briefId) {
      await sleep(80)
      return relayout.getProject(briefId)
    },
    async diagnoseRelayout(briefId) {
      await sleep(300)
      return relayout.diagnose(briefId)
    },
    async planRelayout(briefId, options) {
      // The real optimizer can take tens of seconds; keep the busy state visible in the demo.
      await sleep(1500)
      return relayout.plan(briefId, options)
    },
    async selectRelayoutAlternative(briefId, body) {
      await sleep(150)
      return relayout.select(briefId, body)
    },
    async checkRelayout(briefId, rev) {
      await sleep(300)
      return relayout.check(briefId, rev)
    },
    relayoutReportUrl: (briefId) => relayout.reportUrl(briefId),

    async uploadDrawings(request) {
      await sleep(300)
      return uploads.uploadDrawings(request)
    },
    async listUploadJobs() {
      await sleep(60)
      return uploads.listJobs()
    },
    async getUploadJob(jobId) {
      await sleep(60)
      return uploads.getJob(jobId)
    },
    async submitLayerMapping(jobId, mappings) {
      await sleep(150)
      return uploads.submitLayerMapping(jobId, mappings)
    },
    async listDrawingProfiles() {
      await sleep(40)
      return uploads.listProfiles()
    },
    async uploadMetadata(kind, file) {
      await sleep(250)
      return uploads.uploadMetadata(kind, file)
    },
    async getMetadataStatus() {
      await sleep(60)
      return uploads.metadataStatus()
    },
  }
}
