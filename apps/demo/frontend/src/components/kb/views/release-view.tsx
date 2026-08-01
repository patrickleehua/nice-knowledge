"use client";

import { SnapshotReleaseCard } from "@/components/kb/snapshot-release-card";

export function ReleaseView({ kbId }: { kbId: string }) {
  return (
    <div className="max-w-6xl space-y-4">
      <div>
        <h2 className="font-heading text-lg font-semibold">发布管理</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">
          管理知识库对外生效的版本
        </p>
      </div>

      <SnapshotReleaseCard kbId={kbId} />
    </div>
  );
}
