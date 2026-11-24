import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router";

import { useAssetSearch, useMetrics } from "../api/hooks";
import { t } from "../i18n";
import { useConversations } from "../layout/Conversations";
import { setThemePref, useThemePref } from "../theme";
import { Icon } from "./Icon";
import { useOverlay } from "./overlay";
import { isImeComposing } from "./shortcuts";

type CommandItem = {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: string;
  keywords?: string;
  run: () => void;
};

export function CommandPalette({ open, onClose, roles }: {
  open: boolean;
  onClose: () => void;
  roles: Set<string>;
}) {
  const navigate = useNavigate();
  const theme = useThemePref();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const listId = useId();
  const titleId = useId();

  const handleClose = () => {
    onClose();
    window.setTimeout(() => openerRef.current?.focus?.(), 0);
  };

  useOverlay(open, handleClose);

  useEffect(() => {
    if (!open) return;
    openerRef.current = document.activeElement as HTMLElement | null;
    setQuery("");
    setActive(0);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  const trimmed = query.trim();
  const metrics = useMetrics(open);
  const conversations = useConversations(false);
  const assets = useAssetSearch({ q: trimmed, limit: 8 }, open && trimmed.length >= 2);

  const isAdmin = roles.has("admin");
  const isSteward = isAdmin || roles.has("steward");
  const canViewUsers = isAdmin || isSteward || roles.has("reviewer");

  const items = useMemo<CommandItem[]>(() => {
    const go = (to: string) => () => {
      handleClose();
      navigate(to);
    };
    const nextTheme = theme === "dark" ? "light" : theme === "light" ? "system" : "dark";
    const themeLabel = nextTheme === "dark" ? t("深色") : nextTheme === "light" ? t("浅色") : t("跟随系统");

    const base: CommandItem[] = [
      { id: "act:ask", group: t("快捷操作"), label: t("新建提问与分析"), hint: "/analysis", icon: "plus", keywords: "ask analysis new", run: go("/analysis") },
      { id: "act:theme", group: t("快捷操作"), label: t("切换界面主题：{{theme}}", { theme: themeLabel }), hint: "Theme", icon: theme === "dark" ? "sun" : "moon", keywords: "theme dark light", run: () => { setThemePref(nextTheme); handleClose(); } },
      ...(isSteward ? [{ id: "act:import", group: t("快捷操作"), label: t("导入血缘 SQL / 脚本"), hint: "/lineage/import", icon: "upload", keywords: "import lineage sql", run: go("/lineage/import") }] : []),
      { id: "nav:workbench", group: t("页面导航"), label: t("工作台"), hint: "/", icon: "home", keywords: "workbench home", run: go("/") },
      { id: "nav:analysis", group: t("页面导航"), label: t("问数与分析"), hint: "/analysis", icon: "ask", keywords: "ask analysis", run: go("/analysis") },
      { id: "nav:experiences", group: t("页面导航"), label: t("个人经验"), hint: "/analysis/experiences", icon: "chip", keywords: "experience", run: go("/analysis/experiences") },
      { id: "nav:tasks", group: t("页面导航"), label: t("定时任务"), hint: "/analysis/tasks", icon: "refresh", keywords: "schedule cron tasks", run: go("/analysis/tasks") },
      { id: "nav:lineage", group: t("页面导航"), label: t("血缘图"), hint: "/lineage", icon: "lineage", keywords: "lineage graph", run: go("/lineage") },
      { id: "nav:metrics", group: t("页面导航"), label: t("指标目录"), hint: "/catalog/metrics", icon: "catalog", keywords: "catalog metrics", run: go("/catalog/metrics") },
      { id: "nav:assets", group: t("页面导航"), label: t("资产目录"), hint: "/catalog/assets", icon: "table", keywords: "catalog assets tables", run: go("/catalog/assets") },
      { id: "nav:domains", group: t("页面导航"), label: t("业务域"), hint: "/catalog/domains", icon: "catalog", keywords: "domains", run: go("/catalog/domains") },
      { id: "nav:joins", group: t("页面导航"), label: t("关联路径"), hint: "/catalog/joins", icon: "link", keywords: "joins", run: go("/catalog/joins") },
      { id: "nav:queries", group: t("页面导航"), label: t("标准查询"), hint: "/catalog/queries", icon: "check", keywords: "vqr golden sql verified query", run: go("/catalog/queries") },
      { id: "nav:changes", group: t("页面导航"), label: t("变更集"), hint: "/changes", icon: "change", keywords: "changesets", run: go("/changes") },
      { id: "nav:records", group: t("页面导航"), label: t("变更记录"), hint: "/changes?view=records", icon: "change", keywords: "change records", run: go("/changes?view=records") },
      { id: "nav:skills", group: t("页面导航"), label: t("技能市场"), hint: "/skills/market", icon: "skill", keywords: "skills market", run: go("/skills/market") },
      { id: "nav:datasources", group: t("页面导航"), label: t("数据源设置"), hint: "/settings/datasources", icon: "db", keywords: "datasource settings", run: go("/settings/datasources") },
      ...(isSteward ? [{ id: "nav:models", group: t("页面导航"), label: t("模型配置"), hint: "/settings/models", icon: "chip", keywords: "model settings", run: go("/settings/models") }] : []),
      ...(canViewUsers ? [{ id: "nav:users", group: t("页面导航"), label: t("用户管理"), hint: "/settings/users", icon: "users", keywords: "users settings", run: go("/settings/users") }] : []),
      { id: "nav:calendar", group: t("页面导航"), label: t("业务日历"), hint: "/settings/calendar", icon: "calendar", keywords: "calendar", run: go("/settings/calendar") },
      ...(isAdmin ? [{ id: "nav:webhooks", group: t("页面导航"), label: t("通知订阅"), hint: "/settings/webhooks", icon: "bell", keywords: "webhooks", run: go("/settings/webhooks") }] : []),
      { id: "nav:mcp", group: t("页面导航"), label: t("MCP 接入"), hint: "/settings/mcp", icon: "link", keywords: "mcp", run: go("/settings/mcp") },
      ...(isAdmin ? [{ id: "nav:auth", group: t("页面导航"), label: t("认证与品牌"), hint: "/settings/auth", icon: "shield", keywords: "auth oidc branding", run: go("/settings/auth") }] : []),
      { id: "nav:security", group: t("页面导航"), label: t("账号安全"), hint: "/account/security", icon: "lock", keywords: "totp pat security", run: go("/account/security") },
    ];

    const metricItems: CommandItem[] = (metrics.data ?? []).map((m) => ({
      id: `metric:${m.id}`,
      group: t("指标"),
      label: m.name,
      hint: m.code,
      icon: "chart",
      keywords: `${m.name} ${m.code} ${(m.aliases ?? []).join(" ")}`,
      run: go(`/catalog/metrics/${m.id}`),
    }));

    const chatItems: CommandItem[] = (conversations.data ?? []).slice(0, 12).map((c) => ({
      id: `chat:${c.id}`,
      group: t("最近对话"),
      label: c.display_title || c.title || c.question,
      hint: `${c.turn_count} ${t("轮")}`,
      icon: "ask",
      keywords: `${c.display_title ?? ""} ${c.title ?? ""} ${c.question}`,
      run: go(`/analysis/runs/${c.latest_run_id}`),
    }));

    const assetItems: CommandItem[] = (assets.data ?? []).map((a) => ({
      id: `asset:${a.id}`,
      group: t("数据资产"),
      label: a.qualified,
      hint: a.datasource.name,
      icon: "table",
      keywords: `${a.qualified} ${a.datasource.name}`,
      run: go(`/catalog/assets/${a.id}`),
    }));

    const lower = trimmed.toLowerCase();
    const matched = [...base, ...metricItems, ...chatItems, ...assetItems].filter((it) => {
      if (!lower) return it.group === t("快捷操作") || it.group === t("页面导航") || it.group === t("最近对话");
      return `${it.label} ${it.hint ?? ""} ${it.keywords ?? ""}`.toLowerCase().includes(lower);
    });
    return matched.slice(0, 24);
  }, [assets.data, canViewUsers, conversations.data, isAdmin, isSteward, metrics.data, navigate, theme, trimmed]);

  useEffect(() => {
    setActive((prev) => (items.length ? Math.min(prev, items.length - 1) : 0));
  }, [items.length]);

  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (isImeComposing(e)) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (items.length) setActive((i) => (i + 1) % items.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) setActive((i) => (i - 1 + items.length) % items.length);
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      if (items.length) setActive(items.length - 1);
    } else if (e.key === "Enter" && items[active]) {
      e.preventDefault();
      items[active].run();
    }
  };

  const activeId = items[active] ? `${listId}-opt-${active}` : undefined;

  return createPortal(
    <div className="av-root av-cmd-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) handleClose(); }}>
      <div className="av-cmd" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId} className="av-sr">{t("命令面板")}</h2>
        <div className="av-cmd__head">
          <Icon name="search" />
          <input
            ref={inputRef}
            type="search"
            value={query}
            placeholder={t("搜索页面、指标、资产或最近对话…")}
            aria-label={t("搜索页面、指标、资产或最近对话…")}
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            onKeyDown={onKeyDown}
          />
          <kbd className="av-kbd">Esc</kbd>
        </div>
        <div ref={listRef} id={listId} className="av-cmd__list" role="listbox" aria-label={t("命令结果")}>
          {items.length === 0 ? (
            <div className="av-cmd__empty">{t("没有匹配的结果")}</div>
          ) : (
            items.map((item, i) => (
              <button
                key={item.id}
                id={`${listId}-opt-${i}`}
                data-idx={i}
                type="button"
                role="option"
                aria-selected={i === active}
                className={`av-cmd__item${i === active ? " is-active" : ""}`}
                onMouseEnter={() => setActive(i)}
                onClick={item.run}
              >
                <Icon name={item.icon} small />
                <span className="av-cmd__label">{item.label}</span>
                <span className="av-cmd__group">{item.group}</span>
                {item.hint ? <span className="av-cmd__hint av-mono">{item.hint}</span> : null}
              </button>
            ))
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
