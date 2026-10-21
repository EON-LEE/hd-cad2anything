// Synthetic domain catalog for mock mode (mirrors src/cad2anything/catalog.py; all data is fictional).

export const MOCK_FACILITY = { code: 'FLAGSHIP', name: '가상 플래그십점' }

export const BUILDING = { width: 120, depth: 80 }

export const MOCK_CATEGORIES: readonly { code: string; name_ko: string; fixtures: readonly string[] }[] = [
  { code: 'LUXURY', name_ko: '명품', fixtures: ['SHOWCASE', 'TABLE', 'MANNEQUIN', 'COUNTER'] },
  { code: 'JEWELRY_WATCH', name_ko: '주얼리·시계', fixtures: ['SHOWCASE', 'COUNTER'] },
  { code: 'COSMETICS', name_ko: '화장품', fixtures: ['SHOWCASE', 'COUNTER', 'TABLE'] },
  { code: 'GLOBAL_FASHION', name_ko: '해외패션', fixtures: ['HANGER', 'TABLE', 'MANNEQUIN', 'FITTING', 'COUNTER'] },
  { code: 'ACCESSORIES', name_ko: '잡화', fixtures: ['SHOWCASE', 'TABLE', 'COUNTER'] },
  { code: 'WOMEN_CONTEMPORARY', name_ko: '여성 컨템포러리', fixtures: ['HANGER', 'TABLE', 'MANNEQUIN', 'FITTING', 'COUNTER'] },
  { code: 'WOMEN_CASUAL', name_ko: '여성 캐주얼', fixtures: ['HANGER', 'TABLE', 'FITTING', 'COUNTER'] },
  { code: 'LINGERIE', name_ko: '란제리', fixtures: ['HANGER', 'TABLE', 'FITTING', 'COUNTER'] },
  { code: 'MEN_FORMAL', name_ko: '남성 정장', fixtures: ['HANGER', 'TABLE', 'MANNEQUIN', 'FITTING', 'COUNTER'] },
  { code: 'MEN_CASUAL', name_ko: '남성 캐주얼', fixtures: ['HANGER', 'TABLE', 'FITTING', 'COUNTER'] },
  { code: 'SPORTS', name_ko: '스포츠', fixtures: ['HANGER', 'GONDOLA', 'TABLE', 'FITTING', 'COUNTER'] },
  { code: 'OUTDOOR', name_ko: '아웃도어', fixtures: ['HANGER', 'GONDOLA', 'TABLE', 'FITTING', 'COUNTER'] },
  { code: 'KIDS', name_ko: '아동', fixtures: ['HANGER', 'GONDOLA', 'TABLE', 'COUNTER'] },
  { code: 'LIVING', name_ko: '리빙', fixtures: ['GONDOLA', 'TABLE', 'SHOWCASE', 'COUNTER'] },
  { code: 'ELECTRONICS', name_ko: '가전', fixtures: ['GONDOLA', 'TABLE', 'COUNTER'] },
  { code: 'FOOD_FRESH', name_ko: '신선식품', fixtures: ['SHOWCASE', 'COUNTER'] },
  { code: 'FOOD_DELI', name_ko: '델리', fixtures: ['SHOWCASE', 'COUNTER'] },
  { code: 'BAKERY', name_ko: '베이커리', fixtures: ['SHOWCASE', 'COUNTER'] },
  { code: 'WINE', name_ko: '와인·주류', fixtures: ['GONDOLA', 'SHOWCASE', 'COUNTER'] },
  { code: 'FOOD_COURT', name_ko: '푸드코트', fixtures: ['COUNTER', 'DINING'] },
  { code: 'CAFE', name_ko: '카페', fixtures: ['COUNTER', 'DINING'] },
  { code: 'RESTAURANT', name_ko: '레스토랑', fixtures: ['COUNTER', 'DINING'] },
  { code: 'CULTURE', name_ko: '문화홀', fixtures: ['TABLE', 'COUNTER'] },
]

export function categoryName(code: string | null | undefined): string {
  return MOCK_CATEGORIES.find((c) => c.code === code)?.name_ko ?? code ?? ''
}

export const FIXTURE_DIMS: Record<string, { name_ko: string; w: number; d: number }> = {
  SHOWCASE: { name_ko: '쇼케이스', w: 1.8, d: 0.6 },
  HANGER: { name_ko: '행거', w: 1.2, d: 0.5 },
  GONDOLA: { name_ko: '곤돌라', w: 1.2, d: 0.9 },
  COUNTER: { name_ko: '카운터', w: 2.0, d: 0.8 },
  TABLE: { name_ko: '진열테이블', w: 1.6, d: 1.0 },
  MANNEQUIN: { name_ko: '마네킹스테이지', w: 1.0, d: 1.0 },
  FITTING: { name_ko: '피팅룸', w: 1.5, d: 1.5 },
  DINING: { name_ko: '식탁', w: 1.2, d: 0.8 },
}

/** Mock floors (the mock world only models two floors). */
export const MOCK_FLOORS = [
  { code: '1F', ordinal: 0, name: '명품·화장품', program: ['LUXURY', 'COSMETICS', 'JEWELRY_WATCH'] },
  { code: '3F', ordinal: 2, name: '여성 컨템포러리', program: ['WOMEN_CONTEMPORARY', 'CAFE', 'ACCESSORIES'] },
] as const

export interface GeneratorSpec {
  name: string
  title_ko: string
  risk_tier: 'high' | 'medium' | 'low'
  approval_mode: 'pre_publish' | 'post_publish'
  outputs: readonly string[]
  triggers: readonly string[]
}

// Mirrors src/cad2anything/generators/specs/*.yaml
export const GENERATOR_SPECS: readonly GeneratorSpec[] = [
  {
    name: 'area_schedule',
    title_ko: '면적표',
    risk_tier: 'high',
    approval_mode: 'pre_publish',
    outputs: ['xlsx', 'csv'],
    triggers: ['add', 'remove', 'move', 'reassign', 'split', 'merge', 'resize', 'walkway_change'],
  },
  {
    name: 'fixture_schedule',
    title_ko: '매대·집기 목록',
    risk_tier: 'medium',
    approval_mode: 'pre_publish',
    outputs: ['xlsx', 'csv'],
    triggers: ['add', 'remove', 'move', 'reassign', 'split', 'merge', 'resize', 'fixture_change'],
  },
  {
    name: 'floor_directory_svg',
    title_ko: '층별 안내도',
    risk_tier: 'low',
    approval_mode: 'post_publish',
    outputs: ['svg'],
    triggers: ['add', 'remove', 'move', 'reassign', 'split', 'merge', 'resize', 'walkway_change'],
  },
  {
    name: 'pos_space_kpi',
    title_ko: 'POS↔공간 매핑과 평효율 기준선',
    risk_tier: 'high',
    approval_mode: 'pre_publish',
    outputs: ['xlsx', 'csv'],
    triggers: ['add', 'remove', 'move', 'reassign', 'split', 'merge', 'resize'],
  },
]

export const RISK_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 }
