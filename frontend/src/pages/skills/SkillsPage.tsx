import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { useState } from "react";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router";

import { api, token, unwrap, type Schemas } from "../../api/client";
import { errorText, useTimeZone } from "../../api/hooks";
import { useDebounced } from "../../components/AssetPicker";
import {
  Banner, ConfirmDialog, Crumbs, Icon, PaneHeader, QueryState, Skeleton, Status, Switch, Tile, useMessage,
} from "../../components/ui";
import { formatShortTime } from "../../format";
import { t } from "../../i18n";
import { useMe, useRoles } from "../../layout/Shell";
import { formatSkillTitle, RejectSkillDialog, SkillDrawer } from "./SkillDrawer";
import { UploadDrawer, type UploadPreset } from "./UploadDrawer";

type Skill = Schemas["SkillOut"];
type SkillSummary = Schemas["SkillSummaryOut"];
type SkillDetail = Schemas["SkillDetailOut"];
type SkillSection = "market" | "enabled" | "mine" | "review";

const SECTIONS = new Set<string>(["market", "enabled", "mine", "review"]);

function MineStatus({ skill }: { skill: Skill }) {
  if (skill.latest_status === "pending") return <Status value="open" label={t("待审核")} />;
  if (skill.latest_status === "rejected") return <Status value="rejected" label={t("已驳回")} />;
  if (skill.latest_status === "withdrawn") return <Status value="archived" label={t("已撤回")} />;
  if (skill.status === "delisted") return <Status value="archived" label={t("已下架")} />;
  if (skill.latest_status === "superseded") return <Status value="superseded" label={t("已被替换")} />;
  return <Status value="approved" label={t("已上架")} />;
}

export function SkillsPage() {
  const { section: rawSection = "market" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const qc = useQueryClient();
  const me = useMe();
  const roles = useRoles();
  const isAdmin = roles.has("admin");
  const tz = useTimeZone();

  const [search, setSearch] = useState("");
  const debouncedQ = useDebounced(search.trim(), 300);
  const [message, setMessage] = useMessage();
  const [uploadState, setUploadState] = useState<{ open: boolean; preset: UploadPreset | null }>({ open: false, preset: null });
  const [withdrawing, setWithdrawing] = useState<Skill | null>(null);
  const [rejectTarget, setRejectTarget] = useState<Skill | null>(null);
  const [approvingId, setApprovingId] = useState<string | null>(null);
  const [togglingIds, setTogglingIds] = useState<Set<string>>(() => new Set());

  const validSection = SECTIONS.has(rawSection);
  const section: SkillSection = validSection ? (rawSection as SkillSection) : "market";

  const summaryQuery = useQuery({
    queryKey: ["skills", "summary"],
    queryFn: ({ signal }) => unwrap(api.GET("/api/skills/summary", { signal })),
  });

  const listQuery = useQuery({
    queryKey: ["skills", "list", section, debouncedQ],
    enabled: validSection && (section !== "review" || isAdmin),
    queryFn: ({ signal }) => unwrap(api.GET("/api/skills", {
      params: { query: { view: section, q: debouncedQ || undefined } }, signal,
    })),
  });

  const invalidateSkills = () => {
    void qc.invalidateQueries({ queryKey: ["skills", "summary"] });
    void qc.invalidateQueries({ queryKey: ["skills", "list"] });
    void qc.invalidateQueries({ queryKey: ["skills", "detail"] });
  };

  const toggleMutation = useMutation({
    mutationFn: ({ skillId, enabled }: { skillId: string; enabled: boolean; onFail?: (msg: string) => void }) =>
      unwrap(api.PUT("/api/skills/{skill_id}/enabled", {
        params: { path: { skill_id: skillId } },
        body: { enabled },
      })),
    onMutate: async ({ skillId, enabled }) => {
      setTogglingIds((prev) => new Set(prev).add(skillId));
      await Promise.all([
        qc.cancelQueries({ queryKey: ["skills", "summary"] }),
        qc.cancelQueries({ queryKey: ["skills", "list"] }),
        qc.cancelQueries({ queryKey: ["skills", "detail", skillId] }),
      ]);
      const prevLists = qc.getQueriesData<Skill[]>({ queryKey: ["skills", "list"] });
      const prevSummary = qc.getQueryData<SkillSummary>(["skills", "summary"]);
      const prevDetail = qc.getQueryData<SkillDetail>(["skills", "detail", skillId]);

      let wasEnabled: boolean | undefined = prevDetail?.enabled;
      if (wasEnabled === undefined) {
        for (const [, list] of prevLists) {
          const hit = list?.find((item) => item.id === skillId);
          if (hit) {
            wasEnabled = hit.enabled;
            break;
          }
        }
      }
      const delta = wasEnabled !== undefined && wasEnabled !== enabled ? (enabled ? 1 : -1) : 0;

      for (const [key, list] of prevLists) {
        if (!list) continue;
        qc.setQueryData<Skill[]>(
          key,
          list.map((item) => item.id === skillId
            ? { ...item, enabled, enabled_users: Math.max(0, item.enabled_users + delta) }
            : item),
        );
      }
      if (prevSummary && delta !== 0) {
        qc.setQueryData<SkillSummary>(["skills", "summary"], {
          ...prevSummary,
          enabled: Math.max(0, prevSummary.enabled + delta),
        });
      }
      if (prevDetail) {
        qc.setQueryData<SkillDetail>(["skills", "detail", skillId], {
          ...prevDetail,
          enabled,
          enabled_users: Math.max(0, prevDetail.enabled_users + delta),
        });
      }
      return { prevLists, prevSummary, prevDetail, skillId };
    },
    onError: (err, vars, ctx) => {
      if (ctx) {
        for (const [key, data] of ctx.prevLists) qc.setQueryData(key, data);
        if (ctx.prevSummary !== undefined) qc.setQueryData(["skills", "summary"], ctx.prevSummary);
        if (ctx.prevDetail !== undefined) qc.setQueryData(["skills", "detail", ctx.skillId], ctx.prevDetail);
      }
      const msg = errorText(err);
      setMessage({ ok: false, text: msg });
      vars.onFail?.(msg);
    },
    onSettled: (_data, _err, { skillId }) => {
      setTogglingIds((prev) => {
        const next = new Set(prev);
        next.delete(skillId);
        return next;
      });
      invalidateSkills();
    },
  });

  if (!validSection) return <Navigate to="/skills/market" replace />;
  if (section === "review" && !me.isPending && !isAdmin) return <Navigate to="/skills/market" replace />;

  const selectedSkillId = searchParams.get("skill") || null;

  const openSkill = (id: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("skill", id);
    setSearchParams(next);
  };

  const closeSkill = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("skill");
    setSearchParams(next, { replace: true });
  };

  const handleToggle = (skillId: string, enabled: boolean, onFail?: (msg: string) => void) => {
    setMessage(null);
    toggleMutation.mutate({ skillId, enabled, onFail });
  };

  async function handleApproveRow(skill: Skill) {
    if (approvingId) return;
    setApprovingId(skill.latest_version_id);
    setMessage(null);
    try {
      await unwrap(api.POST("/api/skills/versions/{version_id}/approve", {
        params: { path: { version_id: skill.latest_version_id } },
      }));
      invalidateSkills();
      setMessage({ ok: true, text: t("已通过并上架") });
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setApprovingId(null);
    }
  }

  const summary = summaryQuery.data;
  const navItems: { key: SkillSection; label: string; count?: number | null }[] = [
    { key: "market", label: t("技能市场"), count: summary?.market },
    { key: "enabled", label: t("我启用的"), count: summary?.enabled },
    { key: "mine", label: t("我上传的"), count: summary?.mine },
    ...(isAdmin ? [{ key: "review" as const, label: t("待审核"), count: summary?.review }] : []),
  ];

  const clearSearchBtn = (
    <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setSearch("")}>
      {t("清除搜索")}
    </button>
  );

  const onRowClick = (id: string) => (e: { target: EventTarget }) => {
    if ((e.target as Element).closest("a, button") || window.getSelection()?.toString()) return;
    openSkill(id);
  };

  return (
    <div className="av-fill av-skills">
      <Crumbs back={{ label: t("工作台"), to: "/" }} items={[t("技能")]} />
      <div className="av-pagehd">
        <div className="av-pagehd__title">
          <h1>{t("技能")}</h1>
          <nav className="av-seg" aria-label={t("技能分区")}>
            {navItems.map((item) => (
              <Link key={item.key} to={`/skills/${item.key}`} aria-current={item.key === section ? "page" : undefined}>
                {item.key === section ? <motion.span layoutId="av-skills-pill" className="av-seg__pill" aria-hidden /> : null}
                <span className="av-seg__label">
                  {item.label}
                  {item.count != null ? <span className="av-skills__seg-count">{item.count}</span> : null}
                </span>
              </Link>
            ))}
          </nav>
        </div>
        <div className="av-actions">
          <label className="av-search av-skills__search">
            <Icon name="search" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("搜索技能")}
              aria-label={t("搜索技能")}
            />
          </label>
          <button
            type="button"
            className="av-btn av-btn--primary"
            onClick={() => setUploadState({ open: true, preset: null })}
          >
            <Icon name="upload" />{t("上传技能")}
          </button>
        </div>
      </div>

      <Banner message={message} />

      {section === "market" || section === "enabled" ? (
        <div className="av-skills__scroll">
          <QueryState
            query={listQuery}
            isEmpty={(items) => !items.length}
            emptyText={
              debouncedQ
                ? t("没有匹配的技能")
                : section === "enabled"
                  ? t("暂无启用的技能")
                  : t("暂无上架的技能")
            }
            emptyAction={
              debouncedQ
                ? clearSearchBtn
                : section === "enabled"
                  ? <Link className="av-btn av-btn--secondary" to="/skills/market">{t("去技能市场")}</Link>
                  : undefined
            }
          >
            {(items) => (
              <div className="av-skills__grid">
                {items.map((s) => (
                  <article
                    key={s.id}
                    className={`av-skill-card${selectedSkillId === s.id ? " is-selected" : ""}`}
                    onClick={onRowClick(s.id)}
                  >
                    <div className="av-skill-card__hd">
                      <Tile tone="ask" icon="skill" size="sm" />
                      <button type="button" className="av-skill-card__name" title={s.name} onClick={() => openSkill(s.id)}>
                        {formatSkillTitle(s.name)}
                      </button>
                      <span className="av-skill-card__sw" onClick={(e) => e.stopPropagation()}>
                        <Switch
                          checked={s.enabled}
                          disabled={s.status !== "listed" || togglingIds.has(s.id)}
                          label={t("为我启用 {{name}}", { name: s.name })}
                          onChange={(next) => handleToggle(s.id, next)}
                        />
                      </span>
                    </div>
                    <p className="av-skill-card__desc">{s.description}</p>
                    <div className="av-skill-card__ft">
                      <span className="av-avatar av-skill-avatar" aria-hidden>{s.owner.display_name.slice(0, 1)}</span>
                      <span className="av-skill-card__owner">{s.owner.display_name}</span>
                      <span className="av-skill-card__meta av-mono">/{s.name}</span>
                      <span className="av-skill-card__meta av-mono">v{s.version ?? s.latest_version}</span>
                      <span className="av-skill-card__meta">
                        <Icon name="users" small />{t("{{count}} 人启用", { count: s.enabled_users })}
                      </span>
                      <span className="av-skills__spacer" />
                      {s.has_scripts ? (
                        <span className="av-tag av-skill-script-tag"><Icon name="lock" small />{t("脚本不执行")}</span>
                      ) : null}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </QueryState>
        </div>
      ) : null}

      {section === "mine" ? (
        <section className="av-pane av-skills__boxed" aria-label={t("我上传的")}>
          <PaneHeader title={t("我上传的")} count={listQuery.isSuccess ? listQuery.data.length : undefined} />
          <div className="av-scroll">
            <QueryState
              query={listQuery}
              isEmpty={(items) => !items.length}
              emptyText={debouncedQ ? t("没有匹配的技能") : t("暂无上传的技能")}
              emptyAction={debouncedQ ? clearSearchBtn : undefined}
            >
              {(items) => (
                <div className="av-table-wrap">
                  <table className="av-table av-skills__table">
                    <thead>
                      <tr>
                        <th>{t("技能")}</th>
                        <th>{t("版本")}</th>
                        <th>{t("状态")}</th>
                        <th>{t("说明 / 驳回理由")}</th>
                        <th className="end">{t("操作")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((s) => {
                        const noteCell = s.latest_status === "rejected" && s.review_note
                          ? s.review_note
                          : s.latest_status === "pending" && s.status === "listed" && s.version != null
                            ? t("审核期间已启用的人继续使用 v{{version}}", { version: s.version })
                            : s.latest_note || s.description;
                        return (
                          <tr key={s.id} className="is-link" onClick={onRowClick(s.id)}>
                            <td>
                              <button type="button" className="av-skills__row-link" title={s.name} onClick={() => openSkill(s.id)}>
                                {formatSkillTitle(s.name)}
                              </button>
                              <div className="av-skills__subdesc av-mono">/{s.name}</div>
                            </td>
                            <td><span className="av-tag">v{s.latest_version}</span></td>
                            <td><MineStatus skill={s} /></td>
                            <td className={s.latest_status === "rejected" && s.review_note ? "av-skills__cell-wrap" : "av-skills__cell-wrap av-muted"}>
                              {noteCell}
                            </td>
                            <td className="end">
                              <div className="av-actions">
                                {s.latest_status === "pending" && (me.data ? s.latest_uploaded_by.id === me.data.id : !token.get() && s.latest_uploaded_by.id === s.owner.id) ? (
                                  <button
                                    type="button"
                                    className="av-btn av-btn--secondary av-btn--sm"
                                    onClick={(e) => { e.stopPropagation(); setWithdrawing(s); }}
                                  >
                                    {t("撤回")}
                                  </button>
                                ) : (
                                  <button
                                    type="button"
                                    className="av-btn av-btn--ghost av-btn--sm"
                                    onClick={(e) => { e.stopPropagation(); openSkill(s.id); }}
                                  >
                                    {t("查看")}
                                  </button>
                                )}
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </QueryState>
          </div>
        </section>
      ) : null}

      {section === "review" ? (
        me.isPending ? <Skeleton rows={4} /> : (
          <section className="av-pane av-skills__boxed" aria-label={t("待审核")}>
            <PaneHeader title={t("待审核")} count={listQuery.isSuccess ? listQuery.data.length : undefined} />
            <div className="av-scroll">
              <QueryState
                query={listQuery}
                isEmpty={(items) => !items.length}
                emptyText={debouncedQ ? t("没有匹配的技能") : t("暂无待审核的技能")}
                emptyAction={debouncedQ ? clearSearchBtn : undefined}
              >
                {(items) => (
                  <div className="av-table-wrap">
                    <table className="av-table av-skills__table">
                      <thead>
                        <tr>
                          <th>{t("技能")}</th>
                          <th>{t("版本")}</th>
                          <th>{t("上传人")}</th>
                          <th>{t("提交时间")}</th>
                          <th>{t("文件")}</th>
                          <th className="end">{t("操作")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((s) => {
                          const isNew = s.version == null;
                          const verLabel = isNew
                            ? t("v{{version}} · 新技能", { version: s.latest_version })
                            : t("v{{version}} · 覆盖", { version: s.latest_version });
                          const subText = s.latest_note || s.description;
                          return (
                            <tr key={s.id} className="is-link" onClick={onRowClick(s.id)}>
                              <td className="av-skills__cell-wrap">
                                <button type="button" className="av-skills__row-link" title={s.name} onClick={() => openSkill(s.id)}>
                                  {formatSkillTitle(s.name)}
                                </button>
                                <div className="av-skills__subdesc av-mono">/{s.name}</div>
                                {subText ? <div className="av-skills__subdesc">{subText}</div> : null}
                              </td>
                              <td><span className="av-tag">{verLabel}</span></td>
                              <td>
                                <span className="av-crumb">
                                  <span className="av-avatar av-skill-avatar" aria-hidden>{s.latest_uploaded_by.display_name.slice(0, 1)}</span>
                                  <span>{s.latest_uploaded_by.display_name}</span>
                                </span>
                              </td>
                              <td className="av-mono">{formatShortTime(s.updated_at, tz)}</td>
                              <td>
                                {s.latest_has_scripts ? (
                                  <span className="av-tag av-skill-script-tag"><Icon name="lock" small />{t("脚本不执行")}</span>
                                ) : (
                                  <span className="av-muted">—</span>
                                )}
                              </td>
                              <td className="end">
                                <div className="av-actions">
                                  <button
                                    type="button"
                                    className="av-btn av-btn--ghost av-btn--sm"
                                    onClick={(e) => { e.stopPropagation(); openSkill(s.id); }}
                                  >
                                    {t("查看")}
                                  </button>
                                  <button
                                    type="button"
                                    className="av-btn av-btn--secondary av-btn--sm"
                                    onClick={(e) => { e.stopPropagation(); setRejectTarget(s); }}
                                  >
                                    {t("驳回")}
                                  </button>
                                  <button
                                    type="button"
                                    className="av-btn av-btn--primary av-btn--sm"
                                    disabled={approvingId === s.latest_version_id}
                                    onClick={(e) => { e.stopPropagation(); void handleApproveRow(s); }}
                                  >
                                    {approvingId === s.latest_version_id ? t("处理中…") : t("通过并上架")}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </QueryState>
            </div>
          </section>
        )
      ) : null}

      {selectedSkillId ? (
        <SkillDrawer
          skillId={selectedSkillId}
          isAdmin={isAdmin}
          preferPending={section === "review"}
          toggling={togglingIds.has(selectedSkillId)}
          onToggleEnabled={handleToggle}
          onClose={closeSkill}
          onUploadNewVersion={(preset) => {
            closeSkill();
            setUploadState({ open: true, preset });
          }}
          onInvalidate={invalidateSkills}
          onPageMessage={setMessage}
        />
      ) : null}

      {uploadState.open ? (
        <UploadDrawer
          preset={uploadState.preset}
          onClose={() => setUploadState({ open: false, preset: null })}
          onSubmitted={() => {
            setUploadState({ open: false, preset: null });
            invalidateSkills();
            setMessage({ ok: true, text: t("已提交审核") });
            navigate("/skills/mine");
          }}
        />
      ) : null}

      {withdrawing ? (
        <ConfirmDialog
          title={t("撤回版本 v{{version}}？", { version: withdrawing.latest_version })}
          body={t("撤回后该版本不再进入审核，如需上架需重新上传。")}
          confirm={t("撤回")}
          danger
          onCancel={() => setWithdrawing(null)}
          onConfirm={async () => {
            await unwrap(api.POST("/api/skills/versions/{version_id}/withdraw", {
              params: { path: { version_id: withdrawing.latest_version_id } },
            }));
            invalidateSkills();
            setWithdrawing(null);
            setMessage({ ok: true, text: t("已撤回") });
          }}
        />
      ) : null}

      {rejectTarget ? (
        <RejectSkillDialog
          name={rejectTarget.name}
          version={rejectTarget.latest_version}
          versionId={rejectTarget.latest_version_id}
          onCancel={() => setRejectTarget(null)}
          onRejected={() => {
            invalidateSkills();
            setRejectTarget(null);
            setMessage({ ok: true, text: t("已驳回") });
          }}
        />
      ) : null}
    </div>
  );
}
