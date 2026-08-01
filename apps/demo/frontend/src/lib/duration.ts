// 摄入耗时的展示口径。后端一律给「秒」(float),前端只负责把它说成人话。

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;

/**
 * 秒 → 中文时长。分档而不是一律 `hh:mm:ss`,是因为用户在这里问的是
 * "还要等多久"这个量级,不是精确读数:
 * - 1 分钟以内只报秒,补一个恒为 0 的分位纯属噪音;
 * - 1 小时以内报到秒,秒位是"任务还活着"的活体证据(图片理解阶段一动不动
 *   和卡死在界面上本来就难区分);
 * - 超过 1 小时秒位已无信息量,报到分即可,免得出现「1 小时 18 分 07 秒」
 *   这种读不动的长串。
 *
 * 非法输入(null / undefined / NaN / Infinity)返回 null,由调用方决定是否
 * 渲染,而不是吐一个 "NaN 秒" 到界面上;负数(浏览器时钟快于服务端时会出现)
 * 钳到 0,秒表绝不能倒着走。
 */
export function formatDuration(
  seconds: number | null | undefined,
): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (!Number.isFinite(seconds)) return null;
  const total = Math.max(0, Math.floor(seconds));
  if (total < SECONDS_PER_MINUTE) return `${total} 秒`;
  if (total < SECONDS_PER_HOUR) {
    const minutes = Math.floor(total / SECONDS_PER_MINUTE);
    return `${minutes} 分 ${total % SECONDS_PER_MINUTE} 秒`;
  }
  const hours = Math.floor(total / SECONDS_PER_HOUR);
  const minutes = Math.floor((total % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
  return `${hours} 小时 ${minutes} 分`;
}

/**
 * 从 ISO 起点到 now 的墙钟秒数,给"处理中"的秒表用:接口只需给出起点,
 * 走动交给浏览器本地算,不必为了让数字动起来每秒重发一次请求。
 *
 * 起点缺失或不可解析时返回 null(而不是 0),这样调用方能区分"还没开工"
 * 和"刚开工 0 秒"。
 */
export function elapsedSecondsSince(
  startedAt: string | null | undefined,
  now: number = Date.now(),
): number | null {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  if (!Number.isFinite(start)) return null;
  // 时钟偏移可能让 now < start,交给 formatDuration 之前就钳到 0
  return Math.max(0, (now - start) / 1000);
}
