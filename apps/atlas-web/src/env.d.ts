interface ImportMetaEnv {
  /** "1" forces the built-in mock fixtures (see README). */
  readonly VITE_MOCK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
