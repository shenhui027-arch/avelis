import { Link } from "react-router";

export function Placeholder({ title }: { title: string }) {
  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{title}</h1></div>
      </div>
      <div className="av-workspace">
        <div className="av-empty">
          <span>此页面在 M3 实现。</span>
          <Link className="av-btn av-btn--secondary" to="/settings/models">模型配置</Link>
        </div>
      </div>
    </div>
  );
}
