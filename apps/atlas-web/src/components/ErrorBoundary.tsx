import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  label: string
  children: ReactNode
}

interface State {
  error: Error | null
}

/** Keeps the rest of the app usable when one region (e.g. the WebGL canvas) throws while rendering. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[atlas] ${this.props.label} failed`, error, info.componentStack)
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children
    return (
      <div className="error-boundary" role="alert">
        <p>
          <strong>{this.props.label}</strong> 표시 중 오류가 발생했습니다: {this.state.error.message}
        </p>
        <button type="button" onClick={() => this.setState({ error: null })}>
          다시 시도
        </button>
      </div>
    )
  }
}
