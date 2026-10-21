import { describe, expect, it } from 'vitest'
import {
  HEATMAP_STOPS,
  NO_DATA_COLOR,
  categoryColorByIndex,
  heatmapColor,
  heatmapGradientCss,
  hexToRgba,
  isDark,
  rgbToHex,
  tint,
} from './colors'

describe('hexToRgba', () => {
  it('parses short and long forms', () => {
    expect(hexToRgba('#fff')).toEqual([255, 255, 255, 255])
    expect(hexToRgba('#e53935')).toEqual([229, 57, 53, 255])
    expect(hexToRgba('e53935', 128)).toEqual([229, 57, 53, 128])
    expect(hexToRgba('#11223380')).toEqual([17, 34, 51, 128])
  })

  it('falls back for invalid input', () => {
    expect(hexToRgba('not-a-color', undefined, [1, 2, 3, 4])).toEqual([1, 2, 3, 4])
    expect(hexToRgba(undefined, 9, [1, 2, 3, 4])).toEqual([1, 2, 3, 9])
  })
})

describe('heatmapColor (sequential scale for normalized 0..1 values)', () => {
  const stops = HEATMAP_STOPS.map((hex) => hexToRgba(hex))

  it('maps the end points to the first and last stops', () => {
    expect(heatmapColor(0)).toEqual(stops[0])
    expect(heatmapColor(1)).toEqual(stops[stops.length - 1])
  })

  it('hits interior stops exactly at their positions', () => {
    for (let i = 0; i < stops.length; i += 1) {
      expect(heatmapColor(i / (stops.length - 1))).toEqual(stops[i])
    }
  })

  it('interpolates linearly between stops', () => {
    const t = 0.5 / (stops.length - 1) // halfway between stop 0 and stop 1
    const expected = [0, 1, 2].map((c) => Math.round((stops[0][c] + stops[1][c]) / 2))
    expect(heatmapColor(t).slice(0, 3)).toEqual(expected)
  })

  it('clamps out-of-range values', () => {
    expect(heatmapColor(-0.4)).toEqual(stops[0])
    expect(heatmapColor(1.7)).toEqual(stops[stops.length - 1])
  })

  it('is monotonically darker as values increase', () => {
    const luminance = (v: number) => {
      const [r, g, b] = heatmapColor(v)
      return 0.299 * r + 0.587 * g + 0.114 * b
    }
    for (let v = 0; v < 1; v += 0.05) expect(luminance(v + 0.05)).toBeLessThan(luminance(v))
  })

  it('returns the no-data color for missing values', () => {
    expect(heatmapColor(null)).toEqual(NO_DATA_COLOR)
    expect(heatmapColor(undefined)).toEqual(NO_DATA_COLOR)
    expect(heatmapColor(Number.NaN)).toEqual(NO_DATA_COLOR)
    expect(heatmapColor(null, 100)[3]).toBe(100)
  })

  it('builds a CSS gradient for the legend with every stop', () => {
    const css = heatmapGradientCss()
    expect(css.startsWith('linear-gradient(to right')).toBe(true)
    for (const hex of HEATMAP_STOPS) expect(css).toContain(hex)
    expect(css).toContain('100%')
  })

  it('flags dark colors for label contrast', () => {
    expect(isDark(heatmapColor(1))).toBe(true)
    expect(isDark(heatmapColor(0))).toBe(false)
  })
})

describe('category colors', () => {
  it('matches the backend golden-ratio HLS palette', () => {
    // Reference values computed with Python's colorsys exactly like
    // cad2anything.generators.spatial._category_color (index 0 = LUXURY).
    expect([0, 1, 2, 3].map(categoryColorByIndex)).toEqual(['#e5a8a8', '#a8bae5', '#cce5a8', '#e5a8de'])
  })

  it('tints toward white and converts back to hex', () => {
    expect(tint([0, 0, 0, 255], 0.5)).toEqual([128, 128, 128, 255])
    expect(rgbToHex([255, 0, 128])).toBe('#ff0080')
  })
})
