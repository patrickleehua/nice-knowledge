/**
 * 知识库层级徽章。颜色单一真源为 shared/ToneBadge(TONE_STYLES),此处只保留
 * 层级 → tone + 文案 的语义映射。原先的第三套 StatusBadge/STATUS_STYLES 已下线,
 * 消费者改用 shared 组件(见 ingest-tab)。
 *
 * 保留本文件而非删除,因 components/projects/retrieve-panel.tsx(不在本次权属内)
 * 仍从此导入 LayerBadge。
 */
import { ToneBadge, type Tone } from "@/components/shared";

const LAYER_META: Record<string, { label: string; tone: Tone }> = {
  tenant: { label: "本组织", tone: "primary" },
  shared: { label: "共享库", tone: "teal" },
  platform: { label: "平台层", tone: "muted" },
};

export function LayerBadge({ layer }: { layer: string }) {
  const meta = LAYER_META[layer];
  return <ToneBadge tone={meta?.tone ?? "muted"}>{meta?.label ?? layer}</ToneBadge>;
}
