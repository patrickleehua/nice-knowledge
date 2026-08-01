export interface NavigablePage {
  id: string;
}

export interface WikiNavigationPage extends NavigablePage {
  page_type: string;
  title: string;
  snapshot_id?: string | null;
}

/**
 * Legacy pages keep their row ID. Snapshot projection IDs change on every
 * release, so those pages use the title identity already used by wikilinks.
 */
export function wikiPageNavigationKey(page: WikiNavigationPage): string {
  return page.snapshot_id
    ? `snapshot:${JSON.stringify([page.page_type, page.title])}`
    : `page:${page.id}`;
}

/** Apply the persisted order while keeping newly added, unlisted pages stable at the end. */
export function sortByNavigationOrder<T extends NavigablePage>(
  pages: readonly T[],
  pageOrder: readonly string[],
  keyOf: (page: T) => string = (page) => page.id,
): T[] {
  const positions = new Map(pageOrder.map((id, index) => [id, index]));
  return [...pages].sort((a, b) => {
    const aIndex = positions.get(keyOf(a));
    const bIndex = positions.get(keyOf(b));
    if (aIndex !== undefined || bIndex !== undefined) {
      return (
        (aIndex ?? Number.MAX_SAFE_INTEGER) -
        (bIndex ?? Number.MAX_SAFE_INTEGER)
      );
    }
    return 0;
  });
}

/** Replace one group's order and serialize the complete page tree for persistence. */
export function buildPageOrder<T extends NavigablePage>(
  orderedTypes: readonly string[],
  groups: ReadonlyMap<string, readonly T[]>,
  changedType: string,
  changedItems: readonly T[],
  keyOf: (page: T) => string = (page) => page.id,
): string[] {
  return orderedTypes.flatMap((type) =>
    (type === changedType ? changedItems : (groups.get(type) ?? [])).map(keyOf),
  );
}
