import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next 16 默认拦截跨 origin 的 dev 资源;允许用 127.0.0.1 访问 localhost 上的 dev server,
  // 否则 HMR/hydration 静默失效(仅影响开发模式)。
  allowedDevOrigins: ["127.0.0.1"],
};

export default nextConfig;
