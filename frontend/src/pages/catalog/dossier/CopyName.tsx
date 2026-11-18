// 表名后的复制图标：复制完整表名（库名.表名），结果通过页面的提示条反馈。
import { errorText } from "../../../api/hooks";
import { Icon } from "../../../components/ui";
import { t } from "../../../i18n";

export function CopyName({ qualified, onMessage }: { qualified: string; onMessage: (m: { ok: boolean; text: string }) => void }) {
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error(t("当前浏览器不支持复制，请选择表名后复制"));
      await navigator.clipboard.writeText(qualified);
      onMessage({ ok: true, text: t("已复制表名 {{name}}", { name: qualified }) });
    } catch (e) { onMessage({ ok: false, text: errorText(e) }); }
  };
  return (
    <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon av-dossier-copy" onClick={copy}
      aria-label={t("复制表名")} data-tip={t("复制表名")}><Icon name="copy" small /></button>
  );
}
