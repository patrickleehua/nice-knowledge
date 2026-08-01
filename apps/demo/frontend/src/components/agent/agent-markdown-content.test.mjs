import assert from "node:assert/strict";
import test from "node:test";
import {
  citationRefFromHref,
  isGeneratedMediaReference,
  linkifyCitations,
  sanitizeAgentMarkdown,
} from "./agent-markdown-content.ts";

test("suppresses internal generated-media links and redundant completion copy", () => {
  assert.equal(
    sanitizeAgentMarkdown(
      "图片已生成：\n\n[下载图片](/genimg/generated.png)",
    ),
    "",
  );
  assert.equal(
    sanitizeAgentMarkdown(
      "已按你的要求生成暖色版本。\n\n![预览](/api/v1/genimg/generated.png)",
    ),
    "已按你的要求生成暖色版本。",
  );
});

test("preserves normal Markdown links", () => {
  const value = "查看 [项目详情](/app/projects/project-1)。";
  assert.equal(sanitizeAgentMarkdown(value), value);
  assert.equal(isGeneratedMediaReference("/genimg/a.png"), true);
  assert.equal(isGeneratedMediaReference("/api/v1/genimg/a.png"), true);
  assert.equal(isGeneratedMediaReference("/app/projects/a"), false);
});

test("links standalone citation markers to their source card", () => {
  assert.equal(
    linkifyCitations("泰国落地签需要 4 张照片[1]。"),
    "泰国落地签需要 4 张照片[1](#cite-1)。",
  );
  assert.equal(
    linkifyCitations("入境新规[1][2]已生效"),
    "入境新规[1](#cite-1)[2](#cite-2)已生效",
  );
  assert.equal(linkifyCitations("见来源[12]"), "见来源[12](#cite-12)");
  assert.equal(
    linkifyCitations("- 材料清单[3]\n- 费用[4]"),
    "- 材料清单[3](#cite-3)\n- 费用[4](#cite-4)",
  );
  assert.equal(sanitizeAgentMarkdown("落地签[1]"), "落地签[1](#cite-1)");
});

test("never rewrites brackets that already mean something in Markdown", () => {
  const cases = [
    // 行内链接与图片
    "查看 [1](https://example.com/a) 与 ![1](https://example.com/a.png)",
    "参考 [项目详情](/app/projects/1?ref=[2])",
    // 脚注与引用式链接
    "脚注写法[^1] 不能被改写",
    "引用式链接 [官方公告][1] 保持原样",
    "[1]: https://consular.mfa.gov.cn/notice",
    // 行内代码
    "数组取值写作 `items[0]` 或 `list[12]`",
    "``双反引号里的 [1] 也不动``",
    // 代码块
    "```python\nvalues = data[1]\nprint(values[2])\n```",
    "~~~\nrows[3]\n~~~",
    // 转义
    "转义的 \\[1\\] 不是引用",
  ];
  for (const value of cases) {
    assert.equal(linkifyCitations(value), value, value);
  }
});

test("citation rewriting is idempotent and resolves anchors", () => {
  const once = linkifyCitations("材料清单[1]");
  assert.equal(linkifyCitations(once), once);
  assert.equal(citationRefFromHref("#cite-1"), 1);
  assert.equal(citationRefFromHref("#cite-12"), 12);
  assert.equal(citationRefFromHref("#cite-0"), null);
  assert.equal(citationRefFromHref("#cite-abc"), null);
  assert.equal(citationRefFromHref("#section-1"), null);
  assert.equal(citationRefFromHref("https://example.com"), null);
  assert.equal(citationRefFromHref(undefined), null);
});

test("citation markers survive mixed prose without leaking into code", () => {
  assert.equal(
    linkifyCitations(
      "落地签需提前准备[1]。\n\n```js\nconst refs = notes[1];\n```\n\n更多见 [官方页面](https://a.gov.cn) 与备注[2]。",
    ),
    "落地签需提前准备[1](#cite-1)。\n\n```js\nconst refs = notes[1];\n```\n\n更多见 [官方页面](https://a.gov.cn) 与备注[2](#cite-2)。",
  );
});
