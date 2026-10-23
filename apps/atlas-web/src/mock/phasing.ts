// Mock construction order ("공사 순서", POST /api/relayout/{brief_id}/phasing): a port of the backend's
// scheduling rules. A fit-out starts only in an empty unit; a moving brand keeps trading in its old unit
// until the new one is ready, so chains of moves run in sequence. Brands moving into each other's units
// form a cycle: the one with the lowest weekly sales leaves first, to a temporary unit (a vacant or
// exiting unit not under construction then) or closed. At most `crews` fit-outs run at once; three
// priority rules are tried and the order with the least lost sales plus entry delay wins. Weeks count
// from week 0.
import { formatKrw } from '../lib/format'
import type { PhasingCycle, PhasingEntry, PhasingStep, RelayoutChange, RelayoutPhasing } from '../types/api'

export interface ConstructionInput {
  altId: string
  changes: readonly RelayoutChange[]
  /** unit code -> brand ID of the base revision (vacant units are left out). */
  occupancy: ReadonlyMap<string, string>
  /** unit code -> area (m²) of every unit on the brief's floors. */
  areas: ReadonlyMap<string, number>
  /** unit code -> expected yearly sales in the base revision. */
  annualSales: ReadonlyMap<string, number>
  baseRev: string
  crews: number
  weeks: number
  ratio: number
  useTemporary: boolean
}

export type ConstructionPlan = Omit<RelayoutPhasing, 'brief_id' | 'alt_id' | 'rev_id' | 'created_at'>

type Row = Omit<PhasingStep, 'step' | 'waits_for'> & { waits: string | null }
type Rule = 'critical' | 'ratio' | 'value'
interface Schedule {
  startAt: Map<string, number>
  endAt: Map<string, number>
}
interface Temporary {
  temporary: string | null
  leave: number
  back: number
  lost: number
}

const RANK: Record<string, number> = { exit: 0, temporary: 1, close: 1, move: 2, entry: 2 }

const num = (value: number) => (Number.isInteger(value) ? String(value) : value.toFixed(1))
const pctText = (ratio: number) => `${Math.round(ratio * 100)}%`
const floorOf = (unit: string | null | undefined) => (unit ?? '').split('-')[0]

function floorRank(floor: string): number {
  const basement = /^B(\d+)/i.exec(floor)
  if (basement) return -Number(basement[1])
  const above = /^(\d+)F/i.exec(floor)
  return above ? Number(above[1]) : 99
}

function compareKeys(a: readonly number[], b: readonly number[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) if (a[i] !== b[i]) return a[i] - b[i]
  return a.length - b.length
}

/** "A ↔ B" for a swap, "A → B → C → A" for a longer cycle. */
const loopText = (brands: readonly string[]) => (brands.length === 2 ? brands.join(' ↔ ') : [...brands, brands[0]].join(' → '))

export function planConstruction(input: ConstructionInput): ConstructionPlan {
  const { changes, occupancy, areas, annualSales, crews, weeks, ratio, useTemporary } = input
  const names = new Map(changes.map((c) => [c.brand_id, c.brand_name]))
  const name = (brand: string) => names.get(brand) ?? brand
  const exits = changes.filter((c) => c.type === 'exit')
  const works = changes.filter((c) => c.type === 'move' || c.type === 'entry')
  const movers = new Map(works.filter((c) => c.type === 'move').map((c) => [c.brand_id, c]))
  const entering = new Set(works.filter((c) => c.type === 'entry').map((c) => c.brand_id))
  const exitUnits = new Set(exits.map((c) => c.from_unit).filter((u): u is string => !!u))
  const weekly = new Map(
    works.map((c) => [c.brand_id, (c.type === 'move' ? (annualSales.get(c.from_unit ?? '') ?? 0) : Math.max(c.delta_krw ?? 0, 0)) / 52]),
  )
  const weeklyOf = (brand: string) => weekly.get(brand) ?? 0

  // Each fit-out waits for the brand now in its unit when that brand moves too.
  const blocker = new Map<string, string | null>()
  const mismatched: string[] = []
  for (const change of works) {
    const target = change.to_unit ?? ''
    const occupant = occupancy.get(target)
    if (occupant && movers.has(occupant) && occupant !== change.brand_id && !exitUnits.has(target)) {
      blocker.set(change.brand_id, occupant)
    } else {
      blocker.set(change.brand_id, null)
      if (occupant && !exitUnits.has(target) && !movers.has(occupant)) mismatched.push(target)
    }
  }
  const cycles: string[][] = []
  const seen = new Map<string, string>()
  for (const first of blocker.keys()) {
    const path: string[] = []
    let node: string | null = first
    while (node !== null && !seen.has(node)) {
      seen.set(node, first)
      path.push(node)
      node = blocker.get(node) ?? null
    }
    if (node !== null && seen.get(node) === first) cycles.push(path.slice(path.indexOf(node)))
  }
  const waitersOf = () => {
    const map = new Map<string, string>()
    for (const [job, parent] of blocker) if (parent !== null) map.set(parent, job)
    return map
  }
  let waiterOf = waitersOf()
  const breakers = new Map<string, string>() // waiting fit-out -> brand that leaves its unit early
  for (const cycle of cycles) {
    const breaker = [...cycle].sort((a, b) => weeklyOf(a) - weeklyOf(b) || a.localeCompare(b))[0]
    const waiter = waiterOf.get(breaker)!
    blocker.set(waiter, null)
    breakers.set(waiter, breaker)
  }
  waiterOf = waitersOf()
  const downstream = (job: string) => {
    const chain: string[] = []
    let node = waiterOf.get(job)
    while (node !== undefined) {
      chain.push(node)
      node = waiterOf.get(node)
    }
    return chain
  }
  const depth = (job: string) => {
    let count = 0
    let node = blocker.get(job) ?? null
    while (node !== null) {
      count += 1
      node = blocker.get(node) ?? null
    }
    return count
  }
  const dependents = new Map([...blocker.keys()].map((job) => [job, downstream(job).length]))
  // Weekly sales that finishing a fit-out unlocks: new brands at the end of its chain.
  const unlocks = new Map(
    [...blocker.keys()].map((job) => [job, [job, ...downstream(job)].filter((j) => entering.has(j)).reduce((sum, j) => sum + weeklyOf(j), 0)]),
  )
  // Fit-outs between a breaker leaving and its new unit opening.
  const chainOf = new Map<string, string>()
  for (const [waiter, breaker] of breakers) {
    let node: string | undefined = waiter
    while (node !== undefined) {
      chainOf.set(node, breaker)
      node = node === breaker ? undefined : waiterOf.get(node)
    }
  }
  const order = new Map(works.map((c, i) => [c.brand_id, i]))

  /** List scheduling: a free crew takes the ready fit-out with the best priority of the rule. */
  const schedule = (rule: Rule): Schedule => {
    const startAt = new Map<string, number>()
    const endAt = new Map<string, number>()
    const left = new Set<string>()
    const crewFree = Array.from({ length: crews }, () => 0)
    const remaining = new Set(blocker.keys())
    while (remaining.size) {
      let crew = 0
      for (let i = 1; i < crews; i += 1) if (crewFree[i] < crewFree[crew]) crew = i
      const now = crewFree[crew]
      let best: { key: number[]; job: string } | null = null
      for (const job of remaining) {
        const parent = blocker.get(job) ?? null
        if (parent !== null && !endAt.has(parent)) continue
        const release = parent !== null ? endAt.get(parent)! : 0
        // A brand that already left its unit waits for the rest of its chain: finish it first.
        const chain = chainOf.get(job)
        const active = chain !== undefined && left.has(chain) ? 1 : 0
        const deps = dependents.get(job) ?? 0
        const value = unlocks.get(job) ?? 0
        const priority = rule === 'critical' ? [deps, value] : rule === 'value' ? [value, deps] : [value / (1 + deps), deps]
        const key = [Math.max(now, release), -active, -priority[0], -priority[1], order.get(job) ?? 0]
        if (!best || compareKeys(key, best.key) < 0) best = { key, job }
      }
      if (!best) break
      const begin = best.key[0]
      startAt.set(best.job, begin)
      endAt.set(best.job, begin + weeks)
      crewFree[crew] = begin + weeks
      const leaving = breakers.get(best.job)
      if (leaving) left.add(leaving)
      remaining.delete(best.job)
    }
    return { startAt, endAt }
  }

  // Temporary space: vacant and exiting units on the brief floors, until their own fit-out starts.
  const targetOf = new Map(works.filter((c) => c.to_unit).map((c) => [c.to_unit!, c.brand_id]))
  const vacant = [...areas.keys()].filter((u) => !occupancy.has(u))
  const spaces = [...new Set([...vacant, ...[...exitUnits].sort()])]

  const temporaries = ({ startAt, endAt }: Schedule): Map<string, Temporary> => {
    const busy = new Map<string, [number, number][]>()
    const chosen = new Map<string, Temporary>()
    for (const [waiter, breaker] of breakers) {
      const leave = startAt.get(waiter) ?? 0
      const back = endAt.get(breaker) ?? leave
      const change = movers.get(breaker)!
      const area = areas.get(change.from_unit ?? '') ?? 0
      const free = spaces.filter((u) => {
        const target = targetOf.get(u)
        return (target === undefined || (startAt.get(target) ?? 0) >= back) && (busy.get(u) ?? []).every(([s, e]) => back <= s || leave >= e)
      })
      let temporary: string | null = null
      if (useTemporary && free.length) {
        temporary = [...free].sort(
          (a, b) =>
            Number(floorOf(a) !== change.from_floor) - Number(floorOf(b) !== change.from_floor) ||
            Math.abs((areas.get(a) ?? 0) - area) - Math.abs((areas.get(b) ?? 0) - area) ||
            a.localeCompare(b),
        )[0]
        busy.set(temporary, [...(busy.get(temporary) ?? []), [leave, back]])
      }
      chosen.set(breaker, { temporary, leave, back, lost: weeklyOf(breaker) * (back - leave) * (temporary ? 1 - ratio : 1) })
    }
    return chosen
  }

  /** Expected sales of new brands lost because a crew was busy (beyond waiting for their chain). */
  const entryDelay = (endAt: Map<string, number>) =>
    [...entering].reduce((sum, j) => sum + weeklyOf(j) * ((endAt.get(j) ?? 0) - weeks * (1 + depth(j))), 0)

  let chosenRun: { key: number[]; run: Schedule; temps: Map<string, Temporary> } | null = null
  for (const rule of ['critical', 'ratio', 'value'] as const) {
    const run = schedule(rule)
    const temps = temporaries(run)
    const cost = [...temps.values()].reduce((sum, t) => sum + t.lost, 0) + entryDelay(run.endAt)
    const key = [Math.round(cost), Math.max(0, ...run.endAt.values())]
    if (!chosenRun || compareKeys(key, chosenRun.key) < 0) chosenRun = { key, run, temps }
  }
  const { startAt, endAt } = chosenRun!.run
  const chosen = chosenRun!.temps

  const rows: Row[] = []
  const cycleRows: PhasingCycle[] = []
  const temporaryOf = new Map<string, string | null>()
  for (const [breaker, { temporary, leave, back, lost }] of chosen) {
    const change = movers.get(breaker)!
    const from = change.from_unit ?? ''
    temporaryOf.set(breaker, temporary)
    const span = back - leave
    const place = temporary ?? from
    rows.push({
      start_week: leave,
      end_week: back,
      action: temporary ? 'temporary' : 'close',
      action_label: temporary ? '임시 매장 영업' : '임시 휴점',
      unit_code: place,
      floor: floorOf(place),
      brand_id: breaker,
      brand_name: name(breaker),
      from_unit: from,
      waits: null,
      closed_weeks: temporary ? 0 : span,
      temporary_unit: temporary,
      lost_krw: lost,
      note:
        `순환을 풀려고 ${from} 매장을 먼저 비워요. 새 매장이 열릴 때까지 ${num(span)}주 동안 ` +
        (temporary ? `임시 매장 ${temporary}에서 평소 매출의 ${pctText(ratio)}로 영업한다고 봤어요.` : '휴점해요.'),
    })
    const cycle = cycles.find((c) => c.includes(breaker)) ?? [breaker]
    cycleRows.push({
      brands: cycle.map(name),
      units: cycle.map((b) => movers.get(b)?.from_unit ?? ''),
      breaker: name(breaker),
      resolved_by: temporary ? 'temporary' : 'close',
      unit_code: temporary,
      weeks: span,
    })
  }
  for (const change of exits) {
    rows.push({
      start_week: 0,
      end_week: 0,
      action: 'exit',
      action_label: '퇴점·철거',
      unit_code: change.from_unit,
      floor: change.from_floor,
      brand_id: change.brand_id,
      brand_name: name(change.brand_id),
      from_unit: change.from_unit,
      waits: null,
      closed_weeks: 0,
      temporary_unit: null,
      lost_krw: 0,
      note: '공사 시작 전에 영업을 마치고 매장을 비워요.',
    })
  }
  for (const change of works) {
    const job = change.brand_id
    const parent = blocker.get(job) ?? null
    const before: string[] = []
    if (parent !== null) before.push(`${name(parent)} 이전 공사가 끝난 뒤 시작해요.`)
    const leaving = breakers.get(job)
    if (leaving) before.push(`${name(leaving)} ${temporaryOf.get(leaving) ? '임시 매장 이전' : '휴점'} 뒤 바로 시작해요.`)
    let note: string
    if (change.type === 'entry') note = `공사가 끝나는 ${num(endAt.get(job) ?? 0)}주차에 문을 열어요.`
    else if (temporaryOf.has(job)) {
      const temporary = temporaryOf.get(job)
      note = temporary ? `임시 매장 ${temporary}에서 영업하다가 공사가 끝나면 옮겨요.` : '휴점했다가 공사가 끝나면 새 매장을 열어요.'
    } else note = `기존 매장 ${change.from_unit}에서 영업하다가 공사가 끝나면 옮겨요.`
    rows.push({
      start_week: startAt.get(job) ?? 0,
      end_week: endAt.get(job) ?? 0,
      action: change.type,
      action_label: change.type === 'move' ? '이전 공사' : '신규 입점 공사',
      unit_code: change.to_unit,
      floor: change.to_floor,
      brand_id: job,
      brand_name: name(job),
      from_unit: change.from_unit,
      waits: parent,
      closed_weeks: 0,
      temporary_unit: null,
      lost_krw: 0,
      note: [...before, note].join(' '),
    })
  }
  rows.sort(
    (a, b) =>
      a.start_week - b.start_week ||
      (RANK[a.action] ?? 3) - (RANK[b.action] ?? 3) ||
      floorRank(a.floor ?? '') - floorRank(b.floor ?? '') ||
      (a.unit_code ?? '').localeCompare(b.unit_code ?? ''),
  )
  const stepOf = new Map<string, number>()
  rows.forEach((row, i) => {
    if (row.action === 'move' || row.action === 'entry') stepOf.set(row.brand_id ?? '', i + 1)
  })
  const steps: PhasingStep[] = rows.map(({ waits, ...row }, i) => ({
    step: i + 1,
    ...row,
    waits_for: waits && stepOf.has(waits) ? [stepOf.get(waits)!] : [],
  }))

  const total = Math.max(0, ...endAt.values())
  const lost = steps.reduce((sum, s) => sum + (s.lost_krw ?? 0), 0)
  const naive = [...movers.keys()].reduce((sum, b) => sum + weeklyOf(b), 0) * weeks
  const delay = entryDelay(endAt)
  const entries: PhasingEntry[] = works
    .filter((c) => c.type === 'entry')
    .map((c) => ({
      brand_id: c.brand_id,
      brand_name: name(c.brand_id),
      unit_code: c.to_unit,
      open_week: endAt.get(c.brand_id) ?? null,
      earliest_week: weeks * (1 + depth(c.brand_id)),
    }))
    .sort((a, b) => (a.open_week ?? 0) - (b.open_week ?? 0) || (a.unit_code ?? '').localeCompare(b.unit_code ?? ''))

  const parts = works.length
    ? [`${input.altId}의 공사 ${works.length}건을 공사팀 ${crews}개로 나눠 ${num(total)}주 만에 끝내요.`]
    : [`${input.altId}에는 공사할 매장이 없어요.`]
  if (movers.size) {
    parts.push(`옮기는 브랜드 ${movers.size}곳 중 ${movers.size - breakers.size}곳은 새 매장 공사가 끝날 때까지 기존 매장에서 영업해요.`)
  }
  for (const row of cycleRows) {
    const loop = loopText(row.brands)
    parts.push(
      row.resolved_by === 'temporary'
        ? `서로 자리를 바꾸는 순환(${loop})은 ${row.breaker} 매장을 ${num(row.weeks ?? 0)}주 동안 임시 매장(${row.unit_code})으로 옮겨서 풀어요.`
        : `서로 자리를 바꾸는 순환(${loop})은 쓸 수 있는 빈 매장이 없어 ${row.breaker} 매장이 ${num(row.weeks ?? 0)}주 동안 휴점해야 해요.`,
    )
  }
  if (entries.length) {
    const first = entries[0].open_week ?? 0
    const last = entries.at(-1)!.open_week ?? 0
    parts.push(`신규 입점 ${entries.length}곳은 ${first === last ? `${num(first)}주차` : `${num(first)}~${num(last)}주차`}에 문을 열어요.`)
  }
  if (movers.size) {
    parts.push(
      `예상 영업 손실은 ${formatKrw(lost)}이에요. 옮기는 브랜드가 모두 공사 기간만큼 문을 닫는다고 본 대안 계산의 가정(${formatKrw(naive)})보다 ${formatKrw(naive - lost)} 적어요.`,
    )
  }
  if (delay >= 1e6) parts.push(`공사팀이 모자라 신규 입점이 늦어지는 기회 손실은 ${formatKrw(delay)}이에요. 공사팀을 늘리면 줄어요.`)
  const caveats = [
    `공사 기간은 매장마다 ${num(weeks)}주로 같다고 봤어요. 실제 기간은 면적과 공사 범위에 따라 달라요.`,
    '옮기는 브랜드는 새 매장 공사가 끝나는 주에 바로 옮긴다고 봤어요. 이사·진열 기간은 넣지 않았어요.',
    `손실은 기준 개정 ${input.baseRev}의 연 매출을 52주로 나눈 주 매출로 계산했어요.`,
    '입점 지연 기회 손실은 앞 공사를 기다리는 기간을 빼고, 공사팀이 모자라 늦어진 기간만 셌어요.',
  ]
  if ([...temporaryOf.values()].some(Boolean)) caveats.push(`임시 매장 매출은 평소의 ${pctText(ratio)}로 봤어요. 임시 매장 공사 비용은 넣지 않았어요.`)
  if (mismatched.length) {
    caveats.push(`기준 개정의 ${[...new Set(mismatched)].sort().join(', ')}에 대안에 없는 브랜드가 있어 비어 있다고 봤어요. 도면이 바뀌었다면 대안을 다시 만드세요.`)
  }
  return {
    params: { crews, fitout_weeks: weeks, temporary_sales_ratio: ratio, use_temporary: useTemporary },
    steps,
    total_weeks: total,
    lost_krw: Math.round(lost),
    naive_lost_krw: Math.round(naive),
    saved_krw: Math.round(naive - lost),
    entry_delay_krw: Math.round(delay),
    entries,
    cycles: cycleRows,
    summary: parts.join(' '),
    caveats,
  }
}
