// 资产安全策略标签页：列级敏感分类与脱敏配置、行级 RLS 谓词规则管理。
import { useState } from "react";

import { api, unwrap } from "../../api/client";
import {
  errorText, useAction, useAssetSecurityPolicies, useDomains,
  type AssetColumnPolicy, type AssetRlsPolicy,
} from "../../api/hooks";
import { Banner, Hint, Pill, QueryState, Select, Status, useMessage } from "../../components/ui";
import { t } from "../../i18n";
import { useRoles } from "../../layout/Shell";

const ALL_ROLES = ["admin", "steward", "reviewer", "analyst"] as const;

export function AssetSecurityTab({ assetId }: { assetId: string }) {
  const sec = useAssetSecurityPolicies(assetId);
  const roles = useRoles();
  const domains = useDomains();
  const canEdit = roles.has("steward") || roles.has("admin");
  const [message, setMessage] = useMessage();

  const [editingCol, setEditingCol] = useState<string | null>(null);
  const [colCls, setColCls] = useState<"pii" | "sensitive" | "confidential">("pii");
  const [colStrategy, setColStrategy] = useState<"none" | "mask" | "hash" | "redact">("mask");
  const [colExempt, setColExempt] = useState<string[]>([]);
  const [colNote, setColNote] = useState("");

  const [rlsName, setRlsName] = useState("");
  const [rlsScopeType, setRlsScopeType] = useState<"all" | "role" | "domain" | "user">("all");
  const [rlsScopeValue, setRlsScopeValue] = useState("");
  const [rlsPredicate, setRlsPredicate] = useState("");

  const saveCol = useAction((column: string) => unwrap(
    api.PUT("/api/assets/{asset_id}/column-policies/{column}", {
      params: { path: { asset_id: assetId, column } },
      body: {
        classification: colCls,
        masking_strategy: colStrategy,
        exempt_roles: colExempt,
        note: colNote.trim() || null,
      },
    }),
  ), ["asset-security-policies"]);

  const removeCol = useAction((column: string) => unwrap(
    api.DELETE("/api/assets/{asset_id}/column-policies/{column}", {
      params: { path: { asset_id: assetId, column } },
    }),
  ), ["asset-security-policies"]);

  const createRls = useAction(() => unwrap(
    api.POST("/api/assets/{asset_id}/rls-policies", {
      params: { path: { asset_id: assetId } },
      body: {
        name: rlsName.trim(),
        scope_type: rlsScopeType,
        scope_value: rlsScopeType === "all" ? null : rlsScopeValue.trim(),
        predicate_sql: rlsPredicate.trim(),
        enabled: true,
      },
    }),
  ), ["asset-security-policies"]);

  const toggleRls = useAction((p: AssetRlsPolicy) => unwrap(
    api.PATCH("/api/assets/{asset_id}/rls-policies/{policy_id}", {
      params: { path: { asset_id: assetId, policy_id: p.id } },
      body: { enabled: !p.enabled },
    }),
  ), ["asset-security-policies"]);

  const deleteRls = useAction((policyId: string) => unwrap(
    api.DELETE("/api/assets/{asset_id}/rls-policies/{policy_id}", {
      params: { path: { asset_id: assetId, policy_id: policyId } },
    }),
  ), ["asset-security-policies"]);

  const clsLabels: Record<string, string> = {
    pii: t("个人身份信息 (PII)"),
    sensitive: t("敏感业务数据"),
    confidential: t("高度机密"),
  };
  const strategyLabels: Record<string, string> = {
    none: t("不脱敏"),
    mask: t("部分掩码"),
    hash: t("HMAC 哈希"),
    redact: t("完全隐藏"),
  };
  const scopeLabels: Record<string, string> = {
    all: t("全部用户"),
    role: t("按角色"),
    domain: t("按业务域"),
    user: t("指定用户"),
  };

  const startEditCol = (colName: string, pol?: AssetColumnPolicy) => {
    setEditingCol(colName);
    setColCls(pol?.classification ?? "pii");
    setColStrategy(pol?.masking_strategy ?? "mask");
    setColExempt(pol?.exempt_roles ?? []);
    setColNote(pol?.note ?? "");
  };

  return (
    <QueryState query={sec}>
      {(data) => {
        const byCol = new Map(data.column_policies.map((p) => [p.column_name.toLowerCase(), p]));
        return (
          <>
            <Banner message={message} />
            <section className="av-panel">
              <div className="av-panel__hd">
                <h2 className="av-panel__title">
                  {t("列级敏感分类与脱敏")}
                  <Hint text={t("配置字段敏感级别与脱敏策略；发送给外部大模型的上下文将始终强制脱敏")} />
                </h2>
              </div>
              <div className="av-table-wrap">
                <table className="av-table av-asset-table">
                  <thead>
                    <tr>
                      <th>{t("字段")}</th>
                      <th>{t("敏感分类")}</th>
                      <th>{t("脱敏方式")}</th>
                      <th>{t("豁免角色")}</th>
                      <th>{t("说明")}</th>
                      <th className="end" />
                    </tr>
                  </thead>
                  <tbody>
                    {data.columns.map((col) => {
                      const pol = byCol.get(col.toLowerCase());
                      const editing = editingCol === col;
                      return (
                        <tr key={col}>
                          <td className="av-mono">{col}</td>
                          <td>
                            {editing ? (
                              <Select value={colCls} label={t("敏感分类")} onChange={(v) => setColCls(v as typeof colCls)}>
                                <option value="pii">{clsLabels.pii}</option>
                                <option value="sensitive">{clsLabels.sensitive}</option>
                                <option value="confidential">{clsLabels.confidential}</option>
                              </Select>
                            ) : pol ? (
                              <Pill tone="warn">{clsLabels[pol.classification] ?? pol.classification}</Pill>
                            ) : (
                              <span className="av-muted">{t("未标记")}</span>
                            )}
                          </td>
                          <td>
                            {editing ? (
                              <Select value={colStrategy} label={t("脱敏方式")} onChange={(v) => setColStrategy(v as typeof colStrategy)}>
                                <option value="mask">{strategyLabels.mask}</option>
                                <option value="hash">{strategyLabels.hash}</option>
                                <option value="redact">{strategyLabels.redact}</option>
                                <option value="none">{strategyLabels.none}</option>
                              </Select>
                            ) : pol ? (
                              <span className="av-tag">{strategyLabels[pol.masking_strategy] ?? pol.masking_strategy}</span>
                            ) : "—"}
                          </td>
                          <td>
                            {editing ? (
                              <div className="av-asset-choices">
                                {ALL_ROLES.map((r) => (
                                  <button key={r} type="button" className="av-btn av-btn--secondary av-btn--sm" aria-pressed={colExempt.includes(r)}
                                    onClick={() => setColExempt(colExempt.includes(r) ? colExempt.filter((x) => x !== r) : [...colExempt, r])}>{r}</button>
                                ))}
                              </div>
                            ) : pol?.exempt_roles.length ? pol.exempt_roles.join(", ") : "—"}
                          </td>
                          <td>
                            {editing ? (
                              <input className="av-input" value={colNote} placeholder={t("备注说明（可选）")} onChange={(e) => setColNote(e.target.value)} />
                            ) : (
                              <span className="av-caption">{pol?.note ?? ""}</span>
                            )}
                          </td>
                          <td className="end">
                            {canEdit ? (
                              editing ? (
                                <div className="av-actions">
                                  <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditingCol(null)}>{t("取消")}</button>
                                  <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={saveCol.isPending}
                                    onClick={() => saveCol.mutate(col, {
                                      onSuccess: () => { setEditingCol(null); setMessage({ ok: true, text: t("已保存字段 {{name}} 的脱敏策略", { name: col }) }); },
                                      onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                                    })}>{t("保存")}</button>
                                </div>
                              ) : (
                                <div className="av-actions">
                                  <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => startEditCol(col, pol)}>{t("配置")}</button>
                                  {pol ? (
                                    <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={removeCol.isPending}
                                      onClick={() => removeCol.mutate(col, {
                                        onSuccess: () => setMessage({ ok: true, text: t("已移除字段 {{name}} 的脱敏策略", { name: col }) }),
                                        onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                                      })}>{t("清除")}</button>
                                  ) : null}
                                </div>
                              )
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="av-panel">
              <div className="av-panel__hd">
                <h2 className="av-panel__title">
                  {t("行级权限")}
                  <Hint text={t("在 SQL 编译与探索生成阶段自动注入 WHERE / JOIN ON 条件；支持 :current_user_id 与 :current_username")} />
                </h2>
              </div>
              <div className="av-table-wrap">
                <table className="av-table av-asset-table">
                  <thead>
                    <tr>
                      <th>{t("名称")}</th>
                      <th>{t("作用域")}</th>
                      <th>{t("过滤条件")}</th>
                      <th>{t("状态")}</th>
                      <th className="end" />
                    </tr>
                  </thead>
                  <tbody>
                    {data.rls_policies.map((p) => (
                      <tr key={p.id}>
                        <td>{p.name}</td>
                        <td>{scopeLabels[p.scope_type] ?? p.scope_type}{p.scope_value ? ` (${p.scope_value})` : ""}</td>
                        <td className="av-mono">{p.predicate_sql}</td>
                        <td><Status value={p.enabled ? "active" : "paused"} label={p.enabled ? t("启用") : t("已停用")} /></td>
                        <td className="end">
                          {canEdit ? (
                            <div className="av-actions">
                              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={toggleRls.isPending}
                                onClick={() => toggleRls.mutate(p, {
                                  onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                                })}>{p.enabled ? t("停用") : t("启用")}</button>
                              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={deleteRls.isPending}
                                onClick={() => deleteRls.mutate(p.id, {
                                  onSuccess: () => setMessage({ ok: true, text: t("已删除行级权限规则") }),
                                  onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                                })}>{t("删除")}</button>
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                    {!data.rls_policies.length ? <tr><td colSpan={5} className="av-muted">{t("尚未配置行级权限规则")}</td></tr> : null}
                  </tbody>
                </table>
              </div>
              {canEdit ? (
                <div className="av-panel__bd av-asset-stack">
                  <span className="av-section-label">{t("新增行级权限规则")}</span>
                  <div className="av-formrow">
                    <label className="av-field"><span className="av-label">{t("名称")}</span><input className="av-input" value={rlsName} placeholder={t("例如：仅查看指定区域")} onChange={(e) => setRlsName(e.target.value)} /></label>
                    <label className="av-field"><span className="av-label">{t("作用域")}</span>
                      <Select value={rlsScopeType} label={t("作用域")} onChange={(v) => { setRlsScopeType(v as typeof rlsScopeType); setRlsScopeValue(""); }}>
                        <option value="all">{scopeLabels.all}</option>
                        <option value="role">{scopeLabels.role}</option>
                        <option value="domain">{scopeLabels.domain}</option>
                        <option value="user">{scopeLabels.user}</option>
                      </Select>
                    </label>
                    {rlsScopeType === "role" ? (
                      <label className="av-field"><span className="av-label">{t("角色")}</span>
                        <Select value={rlsScopeValue} label={t("角色")} onChange={setRlsScopeValue}>
                          <option value="">{t("请选择")}</option>
                          {ALL_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                        </Select>
                      </label>
                    ) : rlsScopeType === "domain" ? (
                      <label className="av-field"><span className="av-label">{t("业务域")}</span>
                        <Select value={rlsScopeValue} label={t("业务域")} onChange={setRlsScopeValue}>
                          <option value="">{t("请选择")}</option>
                          {(domains.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                        </Select>
                      </label>
                    ) : rlsScopeType === "user" ? (
                      <label className="av-field"><span className="av-label">{t("用户名或编号")}</span><input className="av-input" value={rlsScopeValue} placeholder="alice" onChange={(e) => setRlsScopeValue(e.target.value)} /></label>
                    ) : null}
                  </div>
                  <div className="av-formrow">
                    <label className="av-field av-field--grow"><span className="av-label">{t("SQL 谓词条件")}</span>
                      <input className="av-input av-mono" value={rlsPredicate} placeholder="region = 'A' OR owner = :current_username" onChange={(e) => setRlsPredicate(e.target.value)} />
                    </label>
                    <button type="button" className="av-btn av-btn--primary" disabled={!rlsName.trim() || !rlsPredicate.trim() || (rlsScopeType !== "all" && !rlsScopeValue.trim()) || createRls.isPending}
                      onClick={() => createRls.mutate(undefined, {
                        onSuccess: () => {
                          setRlsName("");
                          setRlsPredicate("");
                          setRlsScopeValue("");
                          setMessage({ ok: true, text: t("已添加行级权限规则") });
                        },
                        onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                      })}>{t("添加规则")}</button>
                  </div>
                </div>
              ) : null}
            </section>
          </>
        );
      }}
    </QueryState>
  );
}
