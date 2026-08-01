// 查询词 token 高亮:英文按空格分词,中文原词直配(不分词),大小写不敏感。
// String.split 带单个捕获组时,奇数下标即命中片段,避免有状态的 /g/ regex.test。

const ESCAPE_RE = /[.*+?^${}()|[\]\\]/g;

/** 查询串 → 去重 token(按空白切分;纯中文查询整词作为单 token 直配) */
export function tokenize(query: string): string[] {
  return [...new Set(query.trim().split(/\s+/).filter(Boolean))];
}

export function Highlight({
  text,
  tokens,
}: {
  text: string;
  tokens: string[];
}) {
  if (!text || tokens.length === 0) return <>{text}</>;
  // 长 token 优先,避免短词截断长词的命中(如 "巴黎" vs "巴黎圣母院")
  const pattern = new RegExp(
    `(${[...tokens]
      .sort((a, b) => b.length - a.length)
      .map((t) => t.replace(ESCAPE_RE, "\\$&"))
      .join("|")})`,
    "gi",
  );
  return (
    <>
      {text.split(pattern).map((part, i) =>
        i % 2 === 1 ? (
          <mark
            key={i}
            className="rounded-sm bg-primary/20 px-0.5 font-medium text-foreground"
          >
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}
