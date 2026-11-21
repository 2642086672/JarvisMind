import { Component, type ReactNode } from "react";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  failed: boolean;
}

/**
 * Keeps one broken page from blanking the whole console. The error itself is not
 * shown, because it can contain fragments of data; the user can reload instead.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <div role="alert" className="state state-error">
          <h2>页面出现异常</h2>
          <p>这个页面暂时无法显示。其它页面不受影响，你可以重新加载。</p>
          <button type="button" className="btn btn-secondary" onClick={() => window.location.reload()}>
            重新加载
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
