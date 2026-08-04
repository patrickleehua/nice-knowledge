# 图谱通道增益评测语料

这组文档是**为了验证 `kb_graph_search_enabled` 该不该开**而专门构造的，不是真实业务文档。

## 为什么需要专门造一组

在此之前用真实语料跑过一次，结论是"多跳与单跳增益均为 +0.0pt"。但那个结论**不能读成
"图谱没用"**，只能读成"那个语料测不出图谱有没有用"。三条硬伤：

- 全库只有 7 个正文切片，而 `top_k=10` 一次就能把全库捞完，图谱没有"额外召回"的余地；
- **没有任何一个实体跨文档出现**——而图谱的核心价值恰恰是把散落在不同文档里的同名实体
  串起来，这个前提不成立，图谱就退化成了同文档内的实体关系，那部分内容稀疏/向量本来就能召回；
- 评测集是临时拍脑袋标的，6 条样例不具统计意义。

## 这组语料是怎么设计的

六篇文档围绕一套虚构系统展开，实体名刻意生造（星轨/澜图/砚台/沧澜/云梯/青简），
避免模型用预训练知识绕过检索直接作答。

**关键设计:多跳问题的答案文档里，绝不出现问题起点的实体名。** 否则稀疏检索一把就命中，
图谱永远显不出价值。两条多跳链是这样铺的：

```
星轨网关 ──serves──▶ 澜图鉴权服务 ──supports──▶ 砚台存储
  (星轨网关.md 不提砚台存储，砚台存储.md 不提星轨网关)

云梯调度器 ──▶ 青简日志系统 ──▶ 砚台存储冷层
  (云梯调度器.md 不提砚台存储，砚台存储.md 不提云梯调度器)
```

于是"星轨网关的会话数据最终存在哪里"这个问题，**纯词面和纯语义都够不着答案**：
答案在《砚台存储》里，而那篇文档从头到尾没出现过"星轨网关"四个字。只有沿着图谱走两跳
才能到。这就是要测的那个能力。

实体跨文档分布（对比原语料的 0）：

| 实体 | 出现篇数 |
|---|---|
| 沧澜集群 / 澜图鉴权 | 4 |
| 砚台存储 | 3 |
| 北辰机房 / 青简日志 / 星轨网关 / 云梯调度器 | 2 |

## 实验步骤

**1. 录入并发布**

把这六个 `.md` 上传到一个**新建的空知识库**（不要混进现有库，否则基线不干净），
等摄入完成、实体抽取与审核通过、发布快照。

**2. 先确认图谱真的建起来了**

这一步不能跳。图谱没建成的话，后面量到的只是"图谱为空"，不是"图谱没用"：

```sql
-- 跨文档实体应该有若干条，不能是 0
SELECT ce.canonical_name, count(DISTINCT sd.filename) AS 文档数
FROM canonical_entities ce
JOIN fact_claims fc ON fc.subject_entity_id = ce.id OR fc.object_entity_id = ce.id
JOIN evidence_spans es ON es.fact_claim_id = fc.id
JOIN document_revisions dr ON dr.id = es.revision_id
JOIN source_documents sd ON sd.id = dr.doc_id
GROUP BY 1 HAVING count(DISTINCT sd.filename) > 1 ORDER BY 2 DESC;

-- 直接边(edge_kind='direct')应该覆盖上面那两条链
SELECT s.display_name, e.predicate, d.display_name, e.edge_kind
FROM kb_graph_edges e
JOIN kb_snapshot_entity_nodes s ON s.entity_id = e.src_entity_id
JOIN kb_snapshot_entity_nodes d ON d.entity_id = e.dst_entity_id
WHERE e.edge_kind = 'direct';
```

**3. 校正评测集里的 source_ref**

`cases.json` 里的 `relevant` 按 `文件名#chunk0` 填了预估值，实际切片数取决于分块结果。
录入后先列出真实值再对照修正：

```sql
SELECT source_ref FROM kb_chunks WHERE source_ref NOT LIKE 'page:%' ORDER BY source_ref;
```

一个多跳问题的 `relevant` 应该只填**真正含答案的那个切片**，不要把沿途经过的文档都填上，
否则 recall 会被稀释得看不出差异。

**4. 跑对比**

```bash
python -m nicekit.kb.graph_eval --cases cases.json \
  --org-id <你的org> --kb-id <新建库的id> --top-k 10
```

## 怎么判读

报告按 `query_type` 分层，**不输出总均值**——多跳 +10pt 和单跳 −10pt 混在一个均值里
会相抵成"没有影响"，那正是最需要避免的误读。

| 结果形态 | 含义 | 该怎么做 |
|---|---|---|
| 多跳达标、单跳持平 | 图谱确实带来跨文档串联能力，且不伤简单查询 | 可以开 |
| 多跳达标、单跳明显掉点 | 图谱有用但会给简单查询引噪声 | 别全局开，按 query 意图路由 |
| 多跳也不达标 | 要么图谱没建成（回第 2 步），要么这条链路对你的场景没价值 | 保持关闭 |

判据是 Recall@K 或 nDCG@K 的**绝对百分点**差值（0.62→0.67 记作 +5.0pt），不是相对提升。

## 三个容易踩的坑

**先确认基线是健康的。** 我第一次跑这套评测时单跳分层显示 +25pt，看着图谱效果拔群，
追下去发现是稀疏通道有个 quorum 退化 bug 导致漏召回，图谱只是补上了本该被召回的东西。
基线修好后增益归零。**基线有病时量到的是别处的 bug。**

**评测集别自己出题自己判卷。** 这份 `cases.json` 的 ground truth 是照着文档内容标的，
够用来验证功能通不通，但不足以支撑生产决策。真要拍板，用真实业务里捞出来的问题，
并且让标注的人和设计检索的人不是同一个。

**样本量。** 这里 10 条只够做功能验证。业界建这类 golden set 的参考量是 50–200 条
query-document 对，冻结版本保证跨轮可比，并放进 CI 当回归门禁。
