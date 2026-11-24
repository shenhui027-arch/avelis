import { Component, type ErrorInfo, type ReactNode } from "react";

import { t } from "../i18n";
import { Icon } from "./Icon";

type Props = {
  children: ReactNode;
  resetKey?: string | number;
  label?: string;
  compact?: boolean;
  fallback?: ReactNode;
};

type State = {
  error: Error | null;
};

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidUpdate(prevProps: Props): void {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  override componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // Error details are surfaced cleanly inside the boundary fallback UI.
  }

  private reset = () => {
    this.setState({ error: null });
  };

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback;
    const { compact, label } = this.props;
    return (
      <div className={`av-error-boundary${compact ? " av-error-boundary--compact" : ""}`} role="alert">
        <div className="av-error-boundary__hd">
          <Icon name="info" small />
          <strong>{label ?? t("局部内容渲染异常")}</strong>
        </div>
        <p className="av-error-boundary__msg av-mono">{error.message || t("发生未知错误")}</p>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={this.reset}>
            <Icon name="refresh" small />{t("重试")}
          </button>
          {!compact ? (
            <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => window.location.reload()}>
              {t("刷新页面")}
            </button>
          ) : null}
        </div>
      </div>
    );
  }
}
