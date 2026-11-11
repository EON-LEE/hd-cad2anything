import { useEffect, useId, useMemo, useState, type KeyboardEvent } from 'react'

export interface UnitOption {
  code: string
  name: string
  category: string
}

/** Options shown at once; the rest stay searchable by typing a narrower query. */
const MAX_OPTIONS = 40

/**
 * Keyboard-reachable combobox for selecting a unit on the current floor (R3). The map itself is
 * pointer-only (deck.gl picking), so this gives keyboard and screen-reader users an equivalent path:
 * search by brand name or code, press Enter to select, which dispatches the same selection action a
 * map click uses (so the unit details card opens) and announces the choice.
 */
export function UnitList({
  options,
  selectedCode,
  onSelect,
  floorName,
}: {
  options: readonly UnitOption[]
  selectedCode: string | null
  onSelect: (code: string) => void
  floorName: string
}) {
  const baseId = useId()
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [announce, setAnnounce] = useState('')

  const selected = useMemo(() => options.find((o) => o.code === selectedCode) ?? null, [options, selectedCode])
  const selectedText = selected ? `${selected.name} (${selected.code})` : ''

  // Reflect a selection made elsewhere (e.g. a map click) in the field, without opening the list.
  useEffect(() => {
    setQuery(selectedText)
    setOpen(false)
  }, [selectedText])

  const q = query.trim().toLowerCase()
  const matches = useMemo(() => {
    // An unedited field showing the current selection lists every unit, so the user can browse.
    if (!q || query === selectedText) return options.slice(0, MAX_OPTIONS)
    return options
      .filter((o) => o.code.toLowerCase().includes(q) || o.name.toLowerCase().includes(q) || o.category.toLowerCase().includes(q))
      .slice(0, MAX_OPTIONS)
  }, [options, q, query, selectedText])

  const clamp = (i: number) => Math.max(0, Math.min(matches.length - 1, i))

  const choose = (opt: UnitOption | undefined) => {
    if (!opt) return
    onSelect(opt.code)
    setQuery(`${opt.name} (${opt.code})`)
    setOpen(false)
    setAnnounce(`선택: ${opt.name}, ${opt.code}`)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      if (!open) {
        setOpen(true)
        setActive(0)
      } else {
        setActive((i) => clamp(i + 1))
      }
    } else if (event.key === 'ArrowUp') {
      if (open) {
        event.preventDefault()
        setActive((i) => clamp(i - 1))
      }
    } else if (event.key === 'Enter') {
      if (open && matches.length) {
        event.preventDefault()
        choose(matches[clamp(active)])
      }
    } else if (event.key === 'Escape') {
      if (open) {
        event.preventDefault()
        setOpen(false)
      }
    }
  }

  const disabled = options.length === 0
  const listId = `${baseId}-list`
  const activeOption = open ? matches[clamp(active)] : undefined
  const activeId = activeOption ? `${baseId}-opt-${activeOption.code}` : undefined

  return (
    <div className="unit-finder">
      <label htmlFor={`${baseId}-input`} className="unit-finder-label">
        매장 찾기
      </label>
      <div className="unit-finder-box">
        <input
          id={`${baseId}-input`}
          type="text"
          role="combobox"
          className="unit-finder-input"
          autoComplete="off"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeId}
          placeholder={disabled ? '매장 없음' : `${floorName} 매장 이름·코드로 찾기`}
          disabled={disabled}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setOpen(true)
            setActive(0)
          }}
          onFocus={() => {
            if (!disabled) setOpen(true)
          }}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
        />
        {open && !disabled && (
          <ul id={listId} role="listbox" className="unit-finder-list" aria-label={`${floorName} 매장 목록`}>
            {matches.length === 0 ? (
              <li className="unit-finder-empty">검색 결과가 없어요</li>
            ) : (
              matches.map((opt, i) => (
                <li
                  key={opt.code}
                  id={`${baseId}-opt-${opt.code}`}
                  role="option"
                  aria-selected={opt.code === selectedCode}
                  className={`unit-finder-option${i === active ? ' active' : ''}${opt.code === selectedCode ? ' selected' : ''}`}
                  onMouseDown={(event) => {
                    event.preventDefault()
                    choose(opt)
                  }}
                  onMouseEnter={() => setActive(i)}
                >
                  <span className="unit-finder-name">{opt.name}</span>
                  <span className="unit-finder-meta">
                    {opt.code}
                    {opt.category ? ` · ${opt.category}` : ''}
                  </span>
                </li>
              ))
            )}
          </ul>
        )}
      </div>
      <p className="visually-hidden" aria-live="polite">
        {announce}
      </p>
    </div>
  )
}
