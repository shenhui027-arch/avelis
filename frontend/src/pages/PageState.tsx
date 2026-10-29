// 页面级状态：404、403、路由渲染出错。保留外壳，给出明确的返回入口。
import { isRouteErrorResponse, Link, useRouteError } from "react-router";

function PageState({ code, title, text }: { code: string; title: string; text: string }) {
  return (
    <div className="av-pagestate">
      <div className="av-pagestate__box">
        <span className="av-pagestate__code">{code}</span>
        <h1>{title}</h1>
        <p>{text}</p>
        <span className="av-actions">
          <Link to="/" className="av-btn av-btn--primary">返回工作台</Link>
          <Link to="/catalog" className="av-btn av-btn--secondary">浏览目录</Link>
        </span>
      </div>
    </div>
  );
}

export function NotFound() {
  return <PageState code="404" title="页面不存在" text="链接可能已失效，或对象已被删除。" />;
}

export function Forbidden() {
  return <PageState code="403" title="没有访问权限" text="该内容属于你未加入的业务域。如需访问，请联系业务域负责人或管理员。" />;
}

export function RouteError() {
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />;
  return <PageState code="ERROR" title="页面出错了" text="已记录错误。可以刷新重试，或返回工作台。" />;
}
