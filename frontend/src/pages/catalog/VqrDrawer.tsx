// 标准查询（VQR）创建、编辑与审核抽屉：VerifiedQueries 与问数“存为标准查询”共用。
import { useState } from "react";

import { api, unwrap } from "../../api/client";
import {
  errorText, useAction, useDomains, useMetrics, useUsers, useTimeZone, type VerifiedQuery,
} from "../../api/hooks";
import { Banner, Code, Drawer, Select, Status, useMessage } from "../../components/ui";
import { formatTime } from "../../format";
import { t } from "../../i18n";
import { useMe, useRoles } from "../../layout/Shell";

export type VqrPrefill = {
  domain_id?: string;
  title?: string;
  question?: string;
  sql_text?: string;
  dialect?: string;
  metric_codes?: string[];
  source_answer_id?: string;
  source_run_id?: string;
};

const DIALECTS = [
  { value: "postgres", label: "PostgreSQL" },
  { value: "starrocks", label: "StarRocks" },
  { value: "doris", label: "Doris" },
  { value: "mysql", label: "MySQL" },
  { value: "clickhouse", label: "ClickHouse" },
  { value: "sqlserver", label: "SQL Server" },
  { value: "oracle", label: "Oracle" },
  { value: "hive", label: "Hive" },
  { value: "tidb", label: "TiDB" },
  { value: "trino", label: "Trino" },
];

const splitList = (raw: string, byNewline = false) =>
  raw.split(byNewline ? /\n+/ : /[,，、\s]+/).map((s) => s.trim()).filter(Boolean);

export function VqrEditorDrawer({
  initial,
  prefill,
  onClose,
  onSaved,
}: {
  initial?: VerifiedQuery | null;
  prefill?: VqrPrefill;
  onClose: () => void;
  onSaved?: (vq: VerifiedQuery, submitted: boolean) => void;
}) {
  const domains = useDomains();
  const metrics = useMetrics();
  const isEdit = !!initial;
  const domainList = domains.data ?? [];

  const [domainId, setDomainId] = useState(
    initial?.domain_id ?? prefill?.domain_id ?? (domainList.length === 1 ? domainList[0].id : ""),
  );
  const [title, setTitle] = useState(initial?.title ?? prefill?.title ?? "");
  const [question, setQuestion] = useState(initial?.question ?? prefill?.question ?? "");
  const [sqlText, setSqlText] = useState(initial?.sql_text ?? prefill?.sql_text ?? "");
  const [dialect, setDialect] = useState(initial?.dialect ?? prefill?.dialect ?? "postgres");
  const [metricCodesText, setMetricCodesText] = useState(
    (initial?.metric_codes ?? prefill?.metric_codes ?? []).join(", "),
  );
  const [variantsText, setVariantsText] = useState((initial?.question_variants ?? []).join("\n"));
  const [tagsText, setTagsText] = useState((initial?.tags ?? []).join(", "));
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [message, setMessage] = useMessage();

  const resolvedDomainId = domainId || (domainList.length === 1 ? domainList[0].id : "");

  const saveAction = useAction(async (andSubmit: boolean) => {
    const metric_codes = [...new Set(splitList(metricCodesText))];
    const question_variants = [...new Set(splitList(variantsText, true))];
    const tags = [...new Set(splitList(tagsText))];
    let saved: VerifiedQuery;
    if (isEdit && initial) {
      saved = await unwrap(api.PUT("/api/verified-queries/{vqr_id}", {
        params: { path: { vqr_id: initial.id } },
        body: {
          title: title.trim(),
          question: question.trim(),
          sql_text: sqlText.trim(),
          dialect,
          metric_codes,
          question_variants,
          tags,
          notes: notes.trim() || null,
        },
      }));
    } else {
      saved = await unwrap(api.POST("/api/verified-queries", {
        body: {
          domain_id: resolvedDomainId,
          title: title.trim(),
          question: question.trim(),
          sql_text: sqlText.trim(),
          dialect,
          metric_codes,
          question_variants,
          tags,
          notes: notes.trim() || null,
          source_answer_id: prefill?.source_answer_id ?? null,
          source_run_id: prefill?.source_run_id ?? null,
        },
      }));
    }
    if (andSubmit) {
      saved = await unwrap(api.POST("/api/verified-queries/{vqr_id}/submit", {
        params: { path: { vqr_id: saved.id } },
      }));
    }
    return { saved, submitted: andSubmit };
  }, ["verified-queries"]);

  const canSave = !saveAction.isPending
    && (isEdit || !!resolvedDomainId)
    && !!title.trim()
    && !!question.trim()
    && !!sqlText.trim();

  const handleSave = (andSubmit: boolean) => {
    if (!canSave) return;
    setMessage(null);
    saveAction.mutate(andSubmit, {
      onSuccess: ({ saved, submitted }) => {
        onSaved?.(saved, submitted);
        onClose();
      },
      onError: (err) => setMessage({ ok: false, text: errorText(err) }),
    });
  };

  const addMetricCode = (code: string) => {
    if (!code) return;
    const current = splitList(metricCodesText);
    if (!current.includes(code)) setMetricCodesText([...current, code].join(", "));
  };

  return (
    <Drawer
      title={isEdit ? t("编辑标准查询") : t("新建标准查询")}
      width={600}
      onClose={() => { if (!saveAction.isPending) onClose(); }}
      footer={(
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--ghost" disabled={saveAction.isPending} onClick={onClose}>
            {t("取消")}
          </button>
          <button type="button" className="av-btn av-btn--secondary" disabled={!canSave} onClick={() => handleSave(false)}>
            {saveAction.isPending ? t("保存中…") : t("保存草稿")}
          </button>
          <button type="button" className="av-btn av-btn--primary" disabled={!canSave} onClick={() => handleSave(true)}>
            {saveAction.isPending ? t("提交中…") : t("保存并提交审核")}
          </button>
        </div>
      )}
    >
      <div className="av-vqr-form" aria-busy={saveAction.isPending}>
        <Banner message={message} />
        <div className="av-metric-form__row">
          <label className="av-field">
            <span className="av-label">{t("业务域")} *</span>
            <Select
              label={t("业务域")}
              value={resolvedDomainId}
              disabled={isEdit || saveAction.isPending}
              onChange={setDomainId}
            >
              <option value="">{t("选择业务域")}</option>
              {domainList.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
          </label>
          <label className="av-field">
            <span className="av-label">{t("SQL 方言")}</span>
            <Select label={t("SQL 方言")} value={dialect} disabled={saveAction.isPending} onChange={setDialect}>
              {DIALECTS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
            </Select>
          </label>
        </div>
        <label className="av-field">
          <span className="av-label">{t("标题")} *</span>
          <input
            className="av-input"
            maxLength={200}
            value={title}
            disabled={saveAction.isPending}
            placeholder={t("简明概括该标准查询的用途")}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label className="av-field">
          <span className="av-label">{t("标准问题")} *</span>
          <textarea
            className="av-textarea"
            rows={2}
            maxLength={4000}
            value={question}
            disabled={saveAction.isPending}
            placeholder={t("提问人通常会如何问这个问题")}
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>
        <label className="av-field">
          <span className="av-label">{t("只读 SQL")} *</span>
          <textarea
            className="av-textarea av-mono"
            rows={6}
            maxLength={20000}
            value={sqlText}
            disabled={saveAction.isPending}
            placeholder="SELECT ..."
            onChange={(e) => setSqlText(e.target.value)}
          />
        </label>
        <div className="av-metric-form__row">
          <label className="av-field">
            <span className="av-label">{t("关联指标编码")}</span>
            <input
              className="av-input av-mono"
              value={metricCodesText}
              disabled={saveAction.isPending}
              placeholder="metric_a, metric_b"
              onChange={(e) => setMetricCodesText(e.target.value)}
            />
          </label>
          <label className="av-field">
            <span className="av-label">{t("从目录选取指标")}</span>
            <Select
              label={t("从目录选取指标")}
              value=""
              disabled={saveAction.isPending || !(metrics.data?.length)}
              onChange={addMetricCode}
            >
              <option value="">{t("添加指标编码")}</option>
              {(metrics.data ?? []).map((m) => (
                <option key={m.id} value={m.code}>{m.name} ({m.code})</option>
              ))}
            </Select>
          </label>
        </div>
        <label className="av-field">
          <span className="av-label">{t("常见问法变体（每行一条，选填）")}</span>
          <textarea
            className="av-textarea"
            rows={2}
            value={variantsText}
            disabled={saveAction.isPending}
            onChange={(e) => setVariantsText(e.target.value)}
          />
        </label>
        <label className="av-field">
          <span className="av-label">{t("标签（逗号分隔，选填）")}</span>
          <input
            className="av-input"
            value={tagsText}
            disabled={saveAction.isPending}
            onChange={(e) => setTagsText(e.target.value)}
          />
        </label>
        <label className="av-field">
          <span className="av-label">{t("备注（选填）")}</span>
          <textarea
            className="av-textarea"
            rows={2}
            maxLength={2000}
            value={notes}
            disabled={saveAction.isPending}
            onChange={(e) => setNotes(e.target.value)}
          />
        </label>
      </div>
    </Drawer>
  );
}

export function VqrReviewDrawer({
  vq,
  onClose,
  onUpdated,
}: {
  vq: VerifiedQuery;
  onClose: () => void;
  onUpdated: (vq: VerifiedQuery) => void;
}) {
  const domains = useDomains();
  const users = useUsers();
  const tz = useTimeZone();
  const me = useMe();
  const roles = useRoles();
  const [note, setNote] = useState("");
  const [message, setMessage] = useMessage();

  const domainName = domains.data?.find((d) => d.id === vq.domain_id)?.name ?? vq.domain_id;
  const userName = (id?: string | null) => id ? (users.data?.find((u) => u.id === id)?.display_name ?? id) : "—";

  const selfReview = !!me.data && (me.data.id === vq.created_by || me.data.id === vq.submitted_by);
  const hasReviewerRole = roles.has("reviewer") || roles.has("admin");
  const canReview = me.isSuccess && !selfReview && hasReviewerRole;

  const approve = useAction(() => unwrap(api.POST("/api/verified-queries/{vqr_id}/approve", {
    params: { path: { vqr_id: vq.id } },
    body: { note: note.trim() || null },
  })), ["verified-queries"]);

  const reject = useAction(() => unwrap(api.POST("/api/verified-queries/{vqr_id}/reject", {
    params: { path: { vqr_id: vq.id } },
    body: { note: note.trim() },
  })), ["verified-queries"]);

  const busy = approve.isPending || reject.isPending;

  return (
    <Drawer
      title={vq.title}
      width={620}
      onClose={() => { if (!busy) onClose(); }}
      footer={(
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--secondary" disabled={busy} onClick={onClose}>
            {t("关闭")}
          </button>
          {vq.status === "pending_review" ? (
            <>
              <button
                type="button"
                className="av-btn av-btn--danger"
                disabled={!canReview || !note.trim() || busy}
                onClick={() => {
                  setMessage(null);
                  reject.mutate(undefined, {
                    onSuccess: (updated) => { onUpdated(updated); setNote(""); setMessage({ ok: true, text: t("已驳回") }); },
                    onError: (err) => setMessage({ ok: false, text: errorText(err) }),
                  });
                }}
              >
                {reject.isPending ? t("驳回中…") : t("驳回")}
              </button>
              <button
                type="button"
                className="av-btn av-btn--primary"
                disabled={!canReview || busy}
                onClick={() => {
                  setMessage(null);
                  approve.mutate(undefined, {
                    onSuccess: (updated) => { onUpdated(updated); setNote(""); setMessage({ ok: true, text: t("已批准") }); },
                    onError: (err) => setMessage({ ok: false, text: errorText(err) }),
                  });
                }}
              >
                {approve.isPending ? t("批准中…") : t("批准")}
              </button>
            </>
          ) : null}
        </div>
      )}
    >
      <div className="av-vqr-review">
        <div className="av-vqr-review__head">
          <Status value={vq.status} />
          <span className="av-tag">{domainName}</span>
          <span className="av-tag av-mono">{vq.dialect}</span>
        </div>
        <Banner message={message} />
        <dl className="av-kv">
          <dt>{t("标准问题")}</dt><dd>{vq.question}</dd>
          {vq.question_variants.length ? (
            <>
              <dt>{t("问法变体")}</dt>
              <dd>{vq.question_variants.join(" · ")}</dd>
            </>
          ) : null}
          <dt>{t("关联指标")}</dt>
          <dd className="av-vqr-tags">
            {vq.metric_codes.length ? vq.metric_codes.map((c) => <span key={c} className="av-tag av-mono">{c}</span>) : "—"}
          </dd>
          {vq.tags.length ? (
            <>
              <dt>{t("标签")}</dt>
              <dd className="av-vqr-tags">{vq.tags.map((tg) => <span key={tg} className="av-tag">{tg}</span>)}</dd>
            </>
          ) : null}
          <dt>{t("创建人")}</dt><dd>{userName(vq.created_by)}</dd>
          <dt>{t("提交人")}</dt><dd>{userName(vq.submitted_by)}</dd>
          <dt>{t("更新时间")}</dt><dd className="av-mono">{formatTime(vq.updated_at, tz)}</dd>
          {vq.review_note ? <><dt>{t("审核意见")}</dt><dd>{vq.review_note}</dd></> : null}
          {vq.notes ? <><dt>{t("备注")}</dt><dd>{vq.notes}</dd></> : null}
        </dl>
        <div className="av-vqr-review__sql">
          <span className="av-section-label">{t("只读 SQL")}</span>
          <Code sql={vq.sql_text} />
        </div>
        {vq.status === "pending_review" ? (
          <div className="av-vqr-review__panel" aria-busy={busy}>
            {selfReview ? (
              <span className="av-status av-status--warn">{t("不能审核自己创建或提交的标准查询")}</span>
            ) : !hasReviewerRole ? (
              <span className="av-caption">{t("需要复核人或管理员权限才能审核")}</span>
            ) : null}
            <label className="av-field">
              <span className="av-label">{t("审核意见（驳回时必填）")}</span>
              <textarea
                className="av-textarea"
                rows={3}
                maxLength={1000}
                value={note}
                disabled={!canReview || busy}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}
