// 前端导出 CSV 的共用工具：单元格转义（含公式注入防护）、拼正文（带 BOM，Excel 直接打开不乱码）、触发下载。
// 由分析页取数结果导出与血缘表格导出共用。单测：node src/csv.test.mjs（from frontend/）。

/** 单元格一律加双引号；以 = + - @ 制表符 回车 开头的值前加单引号，防止表格软件当公式执行。 */
export function csvCell(value: unknown): string {
  const raw = value == null ? "" : String(value);
  const safe = /^[=+\-@\t\r]/.test(raw) ? `'${raw}` : raw;
  return `"${safe.replace(/"/g, '""')}"`;
}

/** 二维表转 CSV 正文：开头加 BOM（U+FEFF），单元格经 csvCell，行尾 \r\n，末行后不加换行。 */
export function toCsv(matrix: readonly (readonly unknown[])[]): string {
  return "\uFEFF" + matrix.map((row) => row.map(csvCell).join(",")).join("\r\n");
}

/** 把文本存为文件下载。 */
export function downloadText(content: string, mime: string, filename: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
