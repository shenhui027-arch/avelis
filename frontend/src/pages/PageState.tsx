// 页面级状态：404、403、路由渲染出错。保留外壳，给出明确的返回入口。
import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { t } from "../i18n";

function PageState({ code, title, text }: { code: string; title: string; text: string }) {
  return (
    <div className="av-pagestate">
      <div className="av-pagestate__box">
        <span className="av-pagestate__code">{code}</span>
        <h1>{title}</h1>
        <p>{text}</p>
        <span className="av-actions">
          <Link to="/" className="av-btn av-btn--primary">{t("pageState.home")}</Link>
          <Link to="/catalog" className="av-btn av-btn--secondary">{t("pageState.catalog")}</Link>
        </span>
      </div>
    </div>
  );
}

export function NotFound() {
  return <PageState code="404" title={t("pageState.notFoundTitle")} text={t("pageState.notFoundText")} />;
}

export function Forbidden() {
  return <PageState code="403" title={t("pageState.forbiddenTitle")} text={t("pageState.forbiddenText")} />;
}

export function RouteError() {
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />;
  return <PageState code="ERROR" title={t("pageState.errorTitle")} text={t("pageState.errorText")} />;
}
