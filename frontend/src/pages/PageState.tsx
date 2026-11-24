// 页面级状态：404、403、路由渲染出错。保留外壳，给出明确的返回入口。
import { useEffect } from "react";
import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { t } from "../i18n";

function PageState({ code, title, text, detail, onRetry }: {
  code: string; title: string; text: string; detail?: string; onRetry?: () => void;
}) {
  return (
    <div className="av-pagestate">
      <div className="av-pagestate__box">
        <span className="av-pagestate__code">{code}</span>
        <h1>{title}</h1>
        <p>{text}</p>
        {detail ? <pre className="av-pagestate__detail av-mono">{detail}</pre> : null}
        <span className="av-actions">
          {onRetry ? (
            <button type="button" className="av-btn av-btn--primary" onClick={onRetry}>{t("刷新重试")}</button>
          ) : (
            <Link to="/" className="av-btn av-btn--primary">{t("返回工作台")}</Link>
          )}
          {onRetry ? <Link to="/" className="av-btn av-btn--secondary">{t("返回工作台")}</Link> : null}
          <Link to="/catalog" className="av-btn av-btn--secondary">{t("浏览目录")}</Link>
        </span>
      </div>
    </div>
  );
}

export function NotFound() {
  useEffect(() => { document.title = `${t("页面不存在")} · Avelis`; }, []);
  return <PageState code="404" title={t("页面不存在")} text={t("链接可能已失效，或对象已被删除。")} />;
}

export function Forbidden() {
  useEffect(() => { document.title = `${t("没有访问权限")} · Avelis`; }, []);
  return <PageState code="403" title={t("没有访问权限")} text={t("该内容属于你未加入的业务域。如需访问，请联系业务域负责人或管理员。")} />;
}

function errorMessage(error: unknown): string {
  if (isRouteErrorResponse(error)) {
    const extra = typeof error.data === "string" ? error.data : "";
    return `${error.status} ${error.statusText}${extra ? `: ${extra}` : ""}`.trim();
  }
  if (error instanceof Error) return error.message || error.name;
  return typeof error === "string" ? error : "";
}

export function RouteError() {
  const error = useRouteError();
  const notFound = isRouteErrorResponse(error) && error.status === 404;
  const detail = notFound ? "" : errorMessage(error);
  const chunkError = /dynamically imported module|importing a module script|loading chunk/i.test(detail);
  useEffect(() => {
    if (!notFound) document.title = `${t("页面加载异常")} · Avelis`;
  }, [notFound]);
  // 根路由错误边界脱离 Shell 时保留 .av-root 主题色与盒模型
  return (
    <div className="av-root" style={{ height: "100%" }}>
      {notFound
        ? <NotFound />
        : (
          <PageState
            code={isRouteErrorResponse(error) ? String(error.status) : "ERROR"}
            title={t("页面加载异常")}
            text={chunkError ? t("版本已更新或网络中断，请刷新页面重试。") : t("已记录错误。可以刷新重试，或返回工作台。")}
            detail={detail || undefined}
            onRetry={() => window.location.reload()}
          />
        )}
    </div>
  );
}
