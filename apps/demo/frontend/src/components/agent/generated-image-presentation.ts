export function generatedImageGridClass(count: number): string {
  return count === 1 ? "grid-cols-1" : "grid-cols-1 sm:grid-cols-2";
}

export function generatedImageMaxWidth(width: number, height: number): string {
  const safeWidth = Number.isFinite(width) && width > 0 ? width : 1;
  const safeHeight = Number.isFinite(height) && height > 0 ? height : 1;
  return `${Math.min(44, (26 * safeWidth) / safeHeight)}rem`;
}

export function imageFollowUpPrompt(
  action: "adjust" | "again",
  priorPrompt: string,
): string {
  const reference = priorPrompt.trim()
    ? `上一组的画面方向是：${priorPrompt.trim()}。`
    : "请参考上一组 AI 生成图片的画面方向。";
  return action === "adjust"
    ? `${reference}\n请通过修改提示词重新生成（不是原图编辑）。我的调整要求：`
    : `${reference}\n请再次调用 image_generate，重新生成一组新图片。`;
}
