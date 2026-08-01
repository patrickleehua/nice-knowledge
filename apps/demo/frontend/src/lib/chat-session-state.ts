import type { ChatSessionListOut } from "@/lib/chat";

/**
 * A scope refresh is not proof that the selected conversation was deleted.
 * Keep its last known row until a session-specific request reports that access
 * was removed, or an explicit delete removes it from the cache.
 */
export function preserveSelectedSession(
  previous: ChatSessionListOut | undefined,
  refreshed: ChatSessionListOut,
  selectedSessionId: string | null,
): ChatSessionListOut {
  if (
    !selectedSessionId ||
    refreshed.items.some((item) => item.id === selectedSessionId)
  )
    return refreshed;

  const previousIndex = previous?.items.findIndex(
    (item) => item.id === selectedSessionId,
  );
  if (previousIndex === undefined || previousIndex < 0) return refreshed;

  const selected = previous?.items[previousIndex];
  if (!selected) return refreshed;

  const items = [...refreshed.items];
  items.splice(Math.min(previousIndex, items.length), 0, selected);
  return {
    ...refreshed,
    items,
    total: Math.max(refreshed.total, items.length),
  };
}
