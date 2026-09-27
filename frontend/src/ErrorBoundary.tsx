import { Component, type ErrorInfo, type ReactNode } from 'react'

type Props = { name: string; children: ReactNode }
type State = { message: string | null }

export class PanelErrorBoundary extends Component<Props, State> {
  override state: State = { message: null }

  static getDerivedStateFromError(reason: unknown): State {
    return { message: reason instanceof Error ? reason.message : String(reason) }
  }

  override componentDidCatch(reason: unknown, info: ErrorInfo) {
    console.error(`Panel "${this.props.name}" crashed`, reason, info.componentStack)
  }

  override render() {
    if (this.state.message === null) return this.props.children
    return <section className="panel">
      <div className="section-title"><h2>{this.props.name}</h2><span>error</span></div>
      <div className="notice error">このパネルの表示に失敗しました: {this.state.message}</div>
      <button type="button" className="ghost" onClick={() => this.setState({ message: null })}>再試行</button>
    </section>
  }
}
