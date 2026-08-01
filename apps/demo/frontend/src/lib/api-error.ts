/**
 * API 错误类型。
 *
 * 单独成一个**零依赖**模块:`lib/utils.ts` 的 `errMsg()` 要 `instanceof` 它,
 * 而 utils 会被服务端组件(app/not-found.tsx 等)引用。如果 ApiError 留在
 * `lib/api.ts` 里,utils → api → auth(`useSyncExternalStore`)这条链会把
 * 客户端专有 API 拖进 RSC 图,Next 直接构建失败。
 */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
