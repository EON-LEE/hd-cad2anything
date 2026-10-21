// In-browser implementation of the Atlas API backed by the synthetic mock world.
import { ApiError, type AtlasDataSource } from '../api/client'
import type { Artifact, ChatRequest } from '../types/api'
import { buildMockArtifacts, mockLayoutAlternatives, mockProductivity, planImpact } from './analytics'
import { buildMockScript, playScript, sleep } from './chatScripts'
import { buildMockFloor, buildMockMeta } from './floors'
import { mockJourney } from './journey'
import { changesBetween, getWorld } from './world'

const clone = <T>(value: T): T => structuredClone(value)

export function createMockSource(): AtlasDataSource {
  const world = getWorld()
  const meta = buildMockMeta(world)
  const artifacts = new Map<string, Artifact[]>()
  let runCounter = 0

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
      return clone(meta)
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
  }
}
