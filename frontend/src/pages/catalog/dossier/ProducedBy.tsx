// 它是怎么产出的：视图给出定义；导入的脚本给出文件、行号与写入方式，脚本原文只给能维护这张表的人。
import { useTimeZone, type AssetDetail, type AssetDossier } from "../../../api/hooks";
import { Code, Hint, Tile } from "../../../components/ui";
import { formatDate } from "../../../format";
import { t } from "../../../i18n";

const WRITE_MODE_SENTENCE: Record<string, string> = {
  overwrite: t("每次整表覆盖写入。"), append: t("脚本每次在已有数据后追加。"), create: t("每次删掉重建。"),
};

export function ProducedBy({ a, d }: { a: AssetDetail; d: AssetDossier }) {
  const tz = useTimeZone();
  const produced = d.produced;
  if (!produced) return null;
  const scripts = produced.scripts ?? [];
  const mode = produced.write_mode ? WRITE_MODE_SENTENCE[produced.write_mode] : null;
  return (
    <section className="av-panel av-stage tone-collect av-dossier-card av-dossier-produced" aria-label={t("它是怎么产出的")}>
      <div className="av-dossier-card__hd">
        <h2><Tile icon={produced.kind === "view" ? "view" : "upload"} tone="collect" size="sm" />{t("它是怎么产出的")}</h2>
      </div>
      {produced.kind === "view" ? (
        <div className="av-dossier-produced__item">
          <p>{t("这是一个视图，内容由定义实时算出")}</p>
          {a.definition?.raw_sql ? (
            <details className="av-dossier-details">
              <summary>{t("看视图 SQL")}</summary>
              <Code sql={a.definition.raw_sql} />
            </details>
          ) : null}
        </div>
      ) : (
        <>
          {scripts.map((s, i) => (
            <div key={`${s.job_id}-${s.line_start}-${i}`} className="av-dossier-produced__item">
              <p>
                {t("由脚本 {{file}}（第 {{start}}–{{end}} 行）写入", { file: s.filename, start: s.line_start, end: s.line_end })}
                {mode ? <span className="av-dossier-produced__mode">{mode}</span> : null}
                {s.sql ? null : <Hint text={t("脚本原文只对这张表的负责人和维护者显示")} />}
                <span className="av-caption">{t("导入于 {{date}}", { date: formatDate(s.imported_at, tz) })}</span>
              </p>
              {s.sql ? (
                <details className="av-dossier-details">
                  <summary>{t("看脚本")}</summary>
                  <Code sql={s.sql} raw />
                </details>
              ) : null}
            </div>
          ))}
          {!scripts.length && mode ? <div className="av-dossier-produced__item"><p>{mode}</p></div> : null}
        </>
      )}
    </section>
  );
}
