export function defaultActivityDisclosureOpen({
  streaming,
  waitingForApproval,
  pausedForApproval,
}: {
  streaming: boolean;
  waitingForApproval: boolean;
  pausedForApproval: boolean;
}): boolean {
  return streaming && !waitingForApproval && !pausedForApproval;
}
