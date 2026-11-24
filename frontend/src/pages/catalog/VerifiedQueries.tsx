// 标准查询库（VQR / Golden SQL）：检索、域筛选、状态流转与防自审复核。
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router";

import { api, unwrap } from "../../api/client";
import {
  errorText, useAction, useDomains, useVerifiedQueries, type VerifiedQuery,
} from "../../api/hooks";
import { useDebounced } from "../../components/AssetPicker";
import {
  Banner, Code, ConfirmDialog, Hint, Icon, QueryState, Seg, Select, Status, useMessage,
} from "../../components/ui";
import { t } from "../../i18n";
import { useMe, useRoles } from "../../layout/Shell";
import { VqrEditorDrawer, VqrReviewDrawer } from "./VqrDrawer";

type VqrStatusFilter = "" | "approved" | "pending_review" | "draft" | "rejected";

export function VerifiedQueries() {
  const [searchParams, setSearchParams] = useSearchParams();
  const deepId = searchParams.get("id") ?? "";
  const domains = useDomains();
  const me = useMe();
  const roles = useRoles();

  const [search, setSearch] = useState("");
  const [domainFilter, setDomainFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<VqrStatusFilter>("");
  const debouncedSearch = useDebounced(search.trim());

  const list = useVerifiedQueries({
    q: debouncedSearch || undefined,
    domain_id: domainFilter || undefined,
    status: statusFilter || undefined,
  });

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<VerifiedQuery | null>(null);
  const [reviewing, setReviewing] = useState<VerifiedQuery | null>(null);
  const [archiving, setArchiving] = useState<VerifiedQuery | null>(null);
  const [message, setMessage] = useMessage();

  const submitVqr = useAction((id: string) => unwrap(api.POST("/api/verified-queries/{vqr_id}/submit", {
    params: { path: { vqr_id: id } },
  })), ["verified-queries"]);

  const archiveVqr = useAction((id: string) => unwrap(api.POST("/api/verified-queries/{vqr_id}/archive", {
    params: { path: { vqr_id: id } },
  })), ["verified-queries"]);

  const domainMap = new Map((domains.data ?? []).map((d) => [d.id, d]));
  const canCreate = roles.has("steward") || roles.has("reviewer") || roles.has("admin");

  const canManageDomain = (domainId: string) => {
    if (roles.has("admin")) return true;
    if (!roles.has("steward") && !roles.has("reviewer")) return false;
    const d = domainMap.get(domainId);
    if (!d || !me.data) return false;
    if (d.owner_id === me.data.id) return true;
    return d.members.some((m) => m.user_id === me.data?.id && m.role === "maintainer");
  };

  useEffect(() => {
    if (!deepId || !list.data) return;
    const target = list.data.find((item) => item.id === deepId);
    if (target) setReviewing(target);
  }, [deepId, list.data]);

  const closeReview = () => {
    setReviewing(null);
    if (deepId) {
      const next = new URLSearchParams(searchParams);
      next.delete("id");
      setSearchParams(next, { replace: true });
    }
  };

  const addButton = canCreate ? (
    <button type="button" className="av-btn av-btn--primary" onClick={() => setCreating(true)}>
      <Icon name="plus" />{t("新建标准查询")}
    </button>
  ) : undefined;

  return (
    <div className="av-pane__body av-join-root">
      <div className="av-page av-vqr-page">
        <div className="av-pagehd">
          <div className="av-pagehd__title">
            <h1>{t("标准查询")}</h1>
            <Hint text={t("经审核的只读 SQL 模板；问数与分析会按问题召回作口径参考，仍经由受治理引擎执行")} />
          </div>
          <div className="av-actions">{addButton}</div>
        </div>

        <div className="av-vqr-toolbar">
          <div className="av-search av-vqr-toolbar__search" role="search">
            <Icon name="search" />
            <input
              type="search"
              aria-label={t("搜索标准查询")}
              placeholder={t("搜索标题、问题、指标或标签…")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="av-vqr-toolbar__domain">
            <Select label={t("按业务域筛选")} value={domainFilter} onChange={setDomainFilter}>
              <option value="">{t("全部业务域")}</option>
              {(domains.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
          </div>
          <Seg<VqrStatusFilter>
            label={t("按状态筛选")}
            value={statusFilter}
            onChange={setStatusFilter}
            options={[
              { value: "", label: t("全部") },
              { value: "approved", label: t("已批准") },
              { value: "pending_review", label: t("待复核") },
              { value: "draft", label: t("草稿") },
              { value: "rejected", label: t("已驳回") },
            ]}
          />
        </div>

        <Banner message={message} />

        <QueryState
          query={list}
          isEmpty={(rows) => !rows.length}
          emptyText={debouncedSearch || domainFilter || statusFilter ? t("没有匹配的标准查询") : t("暂无标准查询")}
          emptyAction={addButton}
        >
          {(rows) => (
            <div className="av-vqr-list">
              {rows.map((vq) => {
                const domainName = domainMap.get(vq.domain_id)?.name ?? vq.domain_id;
                const editable = canCreate
                  && (vq.status === "draft" || vq.status === "rejected")
                  && (vq.created_by === me.data?.id || canManageDomain(vq.domain_id));
                const selfReview = !!me.data && (me.data.id === vq.created_by || me.data.id === vq.submitted_by);
                const canReviewAction = vq.status === "pending_review" && (roles.has("reviewer") || roles.has("admin"));
                return (
                  <article key={vq.id} className="av-panel av-vqr-card" data-selected={vq.id === deepId || undefined}>
                    <div className="av-vqr-card__hd">
                      <div className="av-vqr-card__titles">
                        <div className="av-vqr-card__top">
                          <h2>{vq.title}</h2>
                          <Status value={vq.status} />
                          <span className="av-tag">{domainName}</span>
                          <span className="av-tag av-mono">{vq.dialect}</span>
                          {vq.metric_codes.map((code) => (
                            <span key={code} className="av-tag av-mono">{code}</span>
                          ))}
                        </div>
                        <p className="av-vqr-card__question">{vq.question}</p>
                        {vq.review_note ? (
                          <p className="av-caption av-vqr-card__note">
                            {t("审核意见：{{note}}", { note: vq.review_note })}
                          </p>
                        ) : null}
                      </div>
                      <div className="av-actions">
                        <button
                          type="button"
                          className="av-btn av-btn--secondary av-btn--sm"
                          onClick={() => setReviewing(vq)}
                        >
                          {canReviewAction && !selfReview ? t("审核") : t("查看")}
                        </button>
                        {editable ? (
                          <>
                            <button
                              type="button"
                              className="av-btn av-btn--ghost av-btn--sm"
                              onClick={() => setEditing(vq)}
                            >
                              {t("编辑")}
                            </button>
                            <button
                              type="button"
                              className="av-btn av-btn--primary av-btn--sm"
                              disabled={submitVqr.isPending}
                              onClick={() => {
                                setMessage(null);
                                submitVqr.mutate(vq.id, {
                                  onSuccess: () => setMessage({ ok: true, text: t("已提交复核") }),
                                  onError: (err) => setMessage({ ok: false, text: errorText(err) }),
                                });
                              }}
                            >
                              {t("提交审核")}
                            </button>
                          </>
                        ) : null}
                        {vq.status === "approved" && canManageDomain(vq.domain_id) ? (
                          <button
                            type="button"
                            className="av-btn av-btn--ghost av-btn--sm"
                            onClick={() => setArchiving(vq)}
                          >
                            {t("归档")}
                          </button>
                        ) : null}
                      </div>
                    </div>
                    <Code sql={vq.sql_text} />
                  </article>
                );
              })}
            </div>
          )}
        </QueryState>
      </div>

      {creating ? (
        <VqrEditorDrawer
          onClose={() => setCreating(false)}
          onSaved={(saved, submitted) => {
            setMessage({
              ok: true,
              text: submitted ? t("已保存「{{title}}」并提交审核", { title: saved.title }) : t("已保存草稿「{{title}}」", { title: saved.title }),
            });
          }}
        />
      ) : null}

      {editing ? (
        <VqrEditorDrawer
          key={editing.id}
          initial={editing}
          onClose={() => setEditing(null)}
          onSaved={(saved, submitted) => {
            setMessage({
              ok: true,
              text: submitted ? t("已更新「{{title}}」并提交审核", { title: saved.title }) : t("已更新「{{title}}」", { title: saved.title }),
            });
          }}
        />
      ) : null}

      {reviewing ? (
        <VqrReviewDrawer
          key={reviewing.id}
          vq={reviewing}
          onClose={closeReview}
          onUpdated={(updated) => setReviewing(updated)}
        />
      ) : null}

      {archiving ? (
        <ConfirmDialog
          danger
          title={t("归档标准查询「{{title}}」？", { title: archiving.title })}
          body={t("归档后该标准查询将不再参与问数与分析召回。")}
          confirm={t("归档")}
          onCancel={() => setArchiving(null)}
          onConfirm={async () => {
            await archiveVqr.mutateAsync(archiving.id);
            setMessage({ ok: true, text: t("已归档「{{title}}」", { title: archiving.title }) });
            setArchiving(null);
          }}
        />
      ) : null}
    </div>
  );
}
