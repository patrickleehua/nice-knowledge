// [[wikilink]] 纯字符串 transform(KB-5C):
// - [[标题]] / [[标题|别名]] → 标准 markdown 链接,href 用 #wikilink: 特殊前缀,
//   由 markdown-view 的 a 组件拦截解析(存在跳转 / 不存在提示创建)。
// - 跳过代码围栏(``` / ~~~,支持缩进 0-3 格与更长闭栏)与行内 code span
//   (反引号串,闭合串长度不小于开串),保证代码里的 [[x]] 不被误转。

export const WIKILINK_HREF_PREFIX = "#wikilink:";

/** 围栏开栏:行首 0-3 空格 + 至少 3 个 ` 或 ~ */
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;

/** 行内 token:code span(`+ 非贪婪到等长闭合)优先于 wikilink,标题/别名不含 [ ] | 换行 */
const INLINE_TOKEN_RE =
  /(`+).*?\1|\[\[([^[\]|]+?)(?:\|([^[\]]+?))?\]\]/g;

/** 逐行遍历,只对非代码围栏内的行应用 fn(围栏行本身也不处理) */
function mapNonFenceLines(md: string, fn: (line: string) => string): string {
  let fence: { char: string; len: number } | null = null;
  return md
    .split("\n")
    .map((line) => {
      const m = FENCE_RE.exec(line);
      if (m) {
        const char = m[1][0];
        const len = m[1].length;
        if (!fence) {
          fence = { char, len };
        } else if (
          char === fence.char &&
          len >= fence.len &&
          new RegExp(`^ {0,3}${char}{${fence.len},}\\s*$`).test(line)
        ) {
          // 闭栏:同字符、不短于开栏、无 info string
          fence = null;
        }
        return line;
      }
      return fence ? line : fn(line);
    })
    .join("\n");
}

/** 链接文本里转义 markdown 语法字符,避免别名破坏链接结构 */
function escapeLabel(label: string): string {
  return label.replace(/([[\]\\`*_])/g, "\\$1");
}

/** 渲染前调用:把正文中的 wikilink 换成 #wikilink: 链接 */
export function transformWikilinks(md: string): string {
  return mapNonFenceLines(md, (line) =>
    line.replace(INLINE_TOKEN_RE, (raw, ticks, title, alias) => {
      if (ticks) return raw; // 行内 code span 原样保留
      const target = String(title).trim();
      if (!target) return raw;
      const label = (alias ? String(alias) : target).trim() || target;
      return `[${escapeLabel(label)}](${WIKILINK_HREF_PREFIX}${encodeURIComponent(target)})`;
    }),
  );
}

/** 提取正文出链标题(去重,保持出现顺序),供元信息卡计数与 lint 联动 */
export function extractWikilinks(md: string): string[] {
  const seen = new Set<string>();
  mapNonFenceLines(md, (line) => {
    for (const m of line.matchAll(INLINE_TOKEN_RE)) {
      if (m[1]) continue; // code span
      const target = m[2]?.trim();
      if (target) seen.add(target);
    }
    return line;
  });
  return [...seen];
}
