"use client";

import { Dialog } from "@base-ui/react/dialog";
import { Menu } from "@base-ui/react/menu";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Clock3,
  LockKeyhole,
  Shield,
  ShieldCheck,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  CUSTOM_DECISION_OPTIONS,
  CUSTOM_PERMISSION_GROUPS,
  PERMISSION_PROFILE_ORDER,
  PERMISSION_PROFILE_FALLBACK,
  buildFullAccessPermissionUpdate,
  canRevokePermissionGrant,
  completeCustomPermissionRules,
  effectiveGroupDecision,
  formatPermissionExpiry,
  permissionUpdateBase,
  permissionControlUnavailable,
  resolvedPermissionProfileOptions,
  type DeferredSessionPermissionUpdate,
  type PermissionScope,
  type SessionPermissionState,
  type SessionPermissionUpdate,
} from "@/lib/agent-permissions";
import {
  toolLabel,
  type PermissionDecision,
  type PermissionProfile,
  type ToolCategory,
} from "@/lib/chat";
import { cn } from "@/lib/utils";

const popupClass =
  "w-[min(22rem,calc(100vw-1.5rem))] origin-(--transform-origin) rounded-2xl bg-popover p-1.5 text-popover-foreground shadow-[0_18px_55px_rgb(0_0_0/0.16)] ring-1 ring-foreground/10 outline-hidden transition-[transform,opacity] duration-100 data-ending-style:scale-[0.98] data-ending-style:opacity-0 data-starting-style:scale-[0.98] data-starting-style:opacity-0 motion-reduce:transition-none";

function scopeLabel(scope: PermissionScope, hasScope: boolean) {
  if (scope === "organization") return "当前组织";
  if (scope === "resource") return hasScope ? "当前作用域" : "作用域范围";
  return "当前会话";
}

function expiryOptions(maximum: number) {
  const candidates = [15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60];
  const values = candidates.filter((value) => value <= maximum);
  if (!values.includes(maximum)) values.push(maximum);
  return [...new Set(values)].sort((left, right) => left - right);
}

function durationLabel(seconds: number) {
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  const hours = seconds / 3600;
  return Number.isInteger(hours) ? `${hours} 小时` : `${seconds / 60} 分钟`;
}

function ProfileIcon({ profile }: { profile: PermissionProfile }) {
  if (profile === "full_access") return <ShieldCheck className="size-4" />;
  if (profile === "custom") return <SlidersHorizontal className="size-4" />;
  return <Shield className="size-4" />;
}

function PermissionDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-[60] bg-black/20 transition-opacity duration-150 data-ending-style:opacity-0 data-starting-style:opacity-0 supports-backdrop-filter:backdrop-blur-[2px] motion-reduce:transition-none" />
        <Dialog.Popup
          className={cn(
            "fixed z-[61] flex max-h-[min(44rem,calc(100dvh-1rem))] flex-col overflow-hidden bg-popover text-popover-foreground shadow-[0_24px_80px_rgb(0_0_0/0.24)] ring-1 ring-foreground/10 outline-none transition duration-200 data-ending-style:opacity-0 data-starting-style:opacity-0 motion-reduce:transition-none max-md:inset-x-2 max-md:bottom-2 max-md:rounded-[1.5rem] max-md:data-ending-style:translate-y-8 max-md:data-starting-style:translate-y-8 md:top-1/2 md:left-1/2 md:w-[min(34rem,calc(100vw-2rem))] md:-translate-x-1/2 md:-translate-y-1/2 md:rounded-2xl md:data-ending-style:scale-[0.98] md:data-starting-style:scale-[0.98]",
            className,
          )}
        >
          <div className="flex items-start gap-3 border-b border-border/55 px-5 py-4">
            <div className="min-w-0 flex-1">
              <Dialog.Title className="text-base font-semibold tracking-tight">
                {title}
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-xs leading-5 text-muted-foreground">
                {description}
              </Dialog.Description>
            </div>
            <Dialog.Close
              aria-label="关闭"
              className="flex size-8 shrink-0 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-foreground/[0.06] hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
            >
              <X className="size-4" />
            </Dialog.Close>
          </div>
          {children}
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function ProfileRows({
  state,
  selectedProfile,
  disabled,
  onSelect,
}: {
  state: SessionPermissionState;
  selectedProfile: PermissionProfile;
  disabled: boolean;
  onSelect: (profile: PermissionProfile) => void;
}) {
  const options = resolvedPermissionProfileOptions(state);
  return (
    <div className="space-y-0.5">
      {options.map((copy) => {
        const profile = copy.id;
        const active = selectedProfile === profile;
        return (
          <button
            key={profile}
            type="button"
            aria-pressed={active}
            disabled={disabled || !copy.allowed}
            onClick={() => onSelect(profile)}
            className={cn(
              "group flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left outline-none transition-colors hover:bg-foreground/[0.05] focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-55",
              active && "bg-foreground/[0.055]",
              profile === "full_access" &&
                copy.allowed &&
                "text-orange-800 dark:text-orange-200",
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-foreground/[0.055] text-muted-foreground",
                active && "bg-foreground text-background",
                profile === "full_access" &&
                  !active &&
                  "bg-orange-500/10 text-orange-700 dark:text-orange-300",
              )}
            >
              {copy.allowed ? (
                <ProfileIcon profile={profile} />
              ) : (
                <LockKeyhole className="size-3.5" />
              )}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2 text-sm font-medium">
                {copy.label}
                {active && <Check className="ml-auto size-4 shrink-0" />}
              </span>
              <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">
                {copy.restriction ?? copy.description}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function PermissionLockNotice({
  activeRun,
  deferred,
  saving,
}: {
  activeRun: boolean;
  deferred: boolean;
  saving: boolean;
}) {
  if (!activeRun && !deferred && !saving) return null;
  return (
    <div
      role="status"
      className="mx-2 mb-1.5 flex items-start gap-2 rounded-xl bg-foreground/[0.045] px-2.5 py-2 text-[11px] leading-4 text-muted-foreground"
    >
      <Clock3 className="mt-0.5 size-3.5 shrink-0" />
      <span>
        {deferred
          ? "已选择下一轮权限；当前操作结束后会自动保存并生效。"
          : activeRun
            ? "当前轮使用已锁定的权限快照；现在可选择，下一轮生效。"
          : "正在保存权限设置…"}
      </span>
    </div>
  );
}

export function PermissionControl({
  state,
  loading,
  disabled,
  pending,
  deferredUpdate,
  onUpdate,
  onRevokeGrant,
}: {
  state?: SessionPermissionState;
  loading?: boolean;
  disabled?: boolean;
  pending?: boolean;
  deferredUpdate?: DeferredSessionPermissionUpdate | null;
  onUpdate: (update: SessionPermissionUpdate) => Promise<void>;
  onRevokeGrant: (grantId: string) => Promise<void>;
}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [fullOpen, setFullOpen] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [fullScope, setFullScope] = useState<PermissionScope>("session");
  const [fullExpiry, setFullExpiry] = useState(30 * 60);
  const [customRules, setCustomRules] = useState<
    Partial<Record<ToolCategory, PermissionDecision>>
  >({});
  const unavailable = permissionControlUnavailable(state, {
    loading,
    pending,
  });
  const activeRun = !!disabled || !!state?.active_run;
  const profile =
    deferredUpdate?.profile ?? state?.profile ?? "request_approval";
  const profileCopy =
    state?.profile_options.find((option) => option.id === profile) ??
    PERMISSION_PROFILE_FALLBACK[profile];
  const expiry = formatPermissionExpiry(state?.expires_at ?? null);
  const fullExpiryValues = useMemo(
    () =>
      expiryOptions(state?.organization.max_full_access_ttl_seconds ?? 3600),
    [state?.organization.max_full_access_ttl_seconds],
  );

  async function selectProfile(next: PermissionProfile) {
    if (!state || unavailable) return;
    setMobileOpen(false);
    if (next === "full_access") {
      setFullScope(state.scope_id ? "resource" : "session");
      setFullExpiry(
        Math.min(30 * 60, state.organization.max_full_access_ttl_seconds),
      );
      setFullOpen(true);
      return;
    }
    if (next === "custom") {
      setCustomRules(
        deferredUpdate?.profile === "custom"
          ? (deferredUpdate.custom_rules ?? state.custom_rules)
          : state.custom_rules,
      );
      setCustomOpen(true);
      return;
    }
    if (next === profile) return;
    try {
      await onUpdate({
        ...permissionUpdateBase(state),
        profile: next,
        scope: state.scope_id ? state.scope : "session",
      });
    } catch {
      // Mutation owner renders the authoritative error; keep the current mode.
    }
  }

  async function activateFullAccess() {
    if (!state) return;
    const update = buildFullAccessPermissionUpdate(
      state,
      fullScope,
      fullExpiry,
    );
    if (!update) return;
    try {
      await onUpdate(update);
      setFullOpen(false);
    } catch {
      // Keep the warning open so the user can retry with fresh server state.
    }
  }

  async function downgrade() {
    if (!state) return;
    try {
      await onUpdate({
        ...permissionUpdateBase(state),
        profile: "request_approval",
        scope: state.scope_id ? state.scope : "session",
      });
      setFullOpen(false);
    } catch {
      // Keep the dialog open on a stale or rejected update.
    }
  }

  async function saveCustom() {
    if (!state) return;
    const completeRules = completeCustomPermissionRules(customRules);
    try {
      await onUpdate({
        ...permissionUpdateBase(state),
        profile: "custom",
        scope: state.scope_id ? state.scope : "session",
        custom_rules: completeRules,
      });
      setCustomOpen(false);
    } catch {
      // Keep edits visible when the authoritative server rejects an update.
    }
  }

  const trigger = (
    <>
      <ProfileIcon profile={profile} />
      <span className="hidden max-w-36 truncate sm:inline">
        {deferredUpdate ? `下一轮：${profileCopy.label}` : profileCopy.label}
      </span>
      <ChevronDown className="hidden size-3.5 text-muted-foreground sm:block" />
    </>
  );

  if (!state) {
    return (
      <button
        type="button"
        disabled
        aria-label="权限：创建会话后可设置"
        className="flex h-8 items-center gap-1.5 rounded-xl px-2 text-xs text-muted-foreground opacity-55"
      >
        <Shield className="size-4" />
        <span className="hidden sm:inline">请求审批</span>
      </button>
    );
  }

  return (
    <>
      <div className="hidden sm:block">
        <Menu.Root>
          <Menu.Trigger
            aria-label={`权限：${profileCopy.label}`}
            className={cn(
              "flex h-8 max-w-full items-center gap-1.5 rounded-xl px-2 text-xs font-medium text-foreground/80 outline-none transition-colors hover:bg-foreground/[0.055] focus-visible:ring-2 focus-visible:ring-ring/50 data-disabled:cursor-not-allowed data-disabled:opacity-50 data-popup-open:bg-foreground/[0.06]",
              profile === "full_access" &&
                "bg-orange-500/8 text-orange-800 hover:bg-orange-500/12 dark:text-orange-200",
            )}
          >
            {trigger}
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Positioner
              side="top"
              align="start"
              sideOffset={8}
              className="isolate z-50 outline-hidden"
            >
              <Menu.Popup className={popupClass}>
                <Menu.RadioGroup
                  value={profile}
                  onValueChange={(next) =>
                    void selectProfile(next as PermissionProfile)
                  }
                >
                  <Menu.GroupLabel className="flex items-center gap-2 px-3 pt-2 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground">
                    <Shield className="size-3.5" />
                    Agent 权限
                    <span className="ml-auto font-normal">
                      {scopeLabel(state.scope, !!state.scope_id)}
                    </span>
                  </Menu.GroupLabel>
                  <PermissionLockNotice
                    activeRun={activeRun}
                    deferred={!!deferredUpdate}
                    saving={!!pending}
                  />
                  {PERMISSION_PROFILE_ORDER.map((item) => {
                    const option = state.profile_options.find(
                      (candidate) => candidate.id === item,
                    );
                    const copy = option ?? PERMISSION_PROFILE_FALLBACK[item];
                    return (
                      <Menu.RadioItem
                        key={item}
                        value={item}
                        disabled={unavailable || !option?.allowed}
                        closeOnClick
                        className={cn(
                          "flex min-h-12 cursor-default items-start gap-3 rounded-xl px-3 py-2.5 text-sm outline-hidden select-none data-highlighted:bg-foreground/[0.055] data-disabled:pointer-events-none data-disabled:opacity-45",
                          item === "full_access" &&
                            option?.allowed &&
                            "text-orange-800 dark:text-orange-200",
                        )}
                      >
                        <span className="mt-0.5 text-muted-foreground">
                          {option?.allowed ? (
                            <ProfileIcon profile={item} />
                          ) : (
                            <LockKeyhole className="size-4" />
                          )}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-2 font-medium">
                            {copy.label}
                            <Menu.RadioItemIndicator className="ml-auto">
                              <Check className="size-4" />
                            </Menu.RadioItemIndicator>
                          </span>
                          <span className="mt-0.5 block text-xs leading-4 text-muted-foreground">
                            {option?.restriction ?? copy.description}
                          </span>
                        </span>
                      </Menu.RadioItem>
                    );
                  })}
                </Menu.RadioGroup>
                {(expiry || state.grants.length > 0) && (
                  <div className="mt-1 border-t border-border/55 px-3 pt-2 pb-1.5">
                    {expiry && (
                      <p className="flex items-center gap-1.5 text-[11px] text-orange-700 dark:text-orange-300">
                        <Clock3 className="size-3.5" />
                        完全访问于 {expiry} 到期
                      </p>
                    )}
                    {state.grants.map((grant) => (
                      <div
                        key={grant.id}
                        className="mt-1.5 flex items-center gap-2 text-[11px] text-muted-foreground"
                      >
                        <span className="min-w-0 flex-1 truncate">
                          本会话允许 {toolLabel(grant.tool_name ?? "unknown")}
                        </span>
                        <button
                          type="button"
                          disabled={
                            !canRevokePermissionGrant(
                              state,
                              grant.id,
                              !!pending || activeRun,
                            )
                          }
                          onClick={() => {
                            void onRevokeGrant(grant.id).catch(() => undefined);
                          }}
                          className="rounded-md p-1 outline-none hover:bg-foreground/[0.06] hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50"
                          aria-label={`撤销 ${toolLabel(grant.tool_name ?? "unknown")} 授权`}
                        >
                          <Trash2 className="size-3.5" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </Menu.Popup>
            </Menu.Positioner>
          </Menu.Portal>
        </Menu.Root>
      </div>

      <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <Dialog.Trigger
          aria-label={`权限：${profileCopy.label}`}
          className={cn(
            "flex size-8 items-center justify-center rounded-xl text-muted-foreground outline-none hover:bg-foreground/[0.055] focus-visible:ring-2 focus-visible:ring-ring/50 disabled:opacity-50 sm:hidden",
            profile === "full_access" && "text-orange-700 dark:text-orange-300",
          )}
        >
          <ProfileIcon profile={profile} />
        </Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Backdrop className="fixed inset-0 z-[60] bg-black/20 transition-opacity data-ending-style:opacity-0 data-starting-style:opacity-0 motion-reduce:transition-none" />
          <Dialog.Popup className="fixed inset-x-2 bottom-2 z-[61] max-h-[calc(100dvh-1rem)] overflow-y-auto rounded-[1.5rem] bg-popover p-2 text-popover-foreground shadow-2xl ring-1 ring-foreground/10 outline-none transition duration-200 data-ending-style:translate-y-8 data-ending-style:opacity-0 data-starting-style:translate-y-8 data-starting-style:opacity-0 motion-reduce:transition-none">
            <div className="flex items-center px-3 py-2">
              <div>
                <Dialog.Title className="text-sm font-semibold">
                  Agent 权限
                </Dialog.Title>
                <Dialog.Description className="mt-0.5 text-xs text-muted-foreground">
                  {scopeLabel(state.scope, !!state.scope_id)}
                </Dialog.Description>
              </div>
              <Dialog.Close
                className="ml-auto flex size-8 items-center justify-center rounded-full text-muted-foreground outline-none hover:bg-foreground/[0.06] focus-visible:ring-2 focus-visible:ring-ring/50"
                aria-label="关闭"
              >
                <X className="size-4" />
              </Dialog.Close>
            </div>
            <PermissionLockNotice
              activeRun={activeRun}
              deferred={!!deferredUpdate}
              saving={!!pending}
            />
            <ProfileRows
              state={state}
              selectedProfile={profile}
              disabled={unavailable}
              onSelect={(next) => void selectProfile(next)}
            />
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>

      <PermissionDialog
        open={fullOpen}
        onOpenChange={setFullOpen}
        title="启用完全访问（业务范围内）"
        description="Agent 可在下列范围和有效期内免询问执行可委托工具。"
      >
        <div className="overflow-y-auto px-5 py-4">
          <div className="rounded-xl bg-orange-500/9 p-3.5 text-orange-950 ring-1 ring-inset ring-orange-500/20 dark:text-orange-100">
            <div className="flex items-start gap-2.5">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-orange-600 dark:text-orange-300" />
              <div>
                <p className="text-sm font-medium">减少确认，不扩大权限边界</p>
                <p className="mt-1 text-xs leading-5 text-orange-900/75 dark:text-orange-100/70">
                  Agent
                  白名单、组织与角色权限、作用域隔离、预算和必须由人工完成的审核仍然生效。
                </p>
              </div>
            </div>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <label className="space-y-1.5 text-xs font-medium">
              生效范围
              <select
                value={fullScope}
                disabled={unavailable}
                onChange={(event) =>
                  setFullScope(event.target.value as PermissionScope)
                }
                className="h-9 w-full rounded-lg border border-input bg-background px-2.5 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              >
                <option value="session">当前会话</option>
                {state.scope_id &&
                  state.organization.max_scope !== "session" && (
                    <option value="resource">当前作用域</option>
                  )}
              </select>
            </label>
            <label className="space-y-1.5 text-xs font-medium">
              有效期
              <select
                value={fullExpiry}
                disabled={unavailable}
                onChange={(event) => setFullExpiry(Number(event.target.value))}
                className="h-9 w-full rounded-lg border border-input bg-background px-2.5 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              >
                {fullExpiryValues.map((seconds) => (
                  <option key={seconds} value={seconds}>
                    {durationLabel(seconds)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            {state.scope_id
              ? "作用域范围只覆盖当前绑定的业务作用域；跨作用域操作仍会询问或拒绝。"
              : "通用会话可创建新的作用域根，但修改既有作用域仍需匹配作用域授权。"}
          </p>
        </div>
        <div className="flex flex-col-reverse gap-2 border-t border-border/55 bg-muted/25 px-5 py-4 sm:flex-row sm:justify-end">
          {state.profile === "full_access" && (
            <Button
              variant="outline"
              disabled={unavailable}
              onClick={() => void downgrade()}
            >
              降级为请求审批
            </Button>
          )}
          <Button
            disabled={unavailable}
            onClick={() => void activateFullAccess()}
            className="bg-orange-600 text-white hover:bg-orange-700"
          >
            {state.profile === "full_access" ? "更新范围与有效期" : "确认启用"}
          </Button>
        </div>
      </PermissionDialog>

      <PermissionDialog
        open={customOpen}
        onOpenChange={setCustomOpen}
        title="自定义 Agent 权限"
        description="按稳定的业务类别设置，不需要逐个维护工具。组织规则始终优先。"
        className="md:w-[min(38rem,calc(100vw-2rem))]"
      >
        <div className="overflow-y-auto px-5 py-2">
          {CUSTOM_PERMISSION_GROUPS.map((group) => {
            const organizationDenied = group.categories.some((category) =>
              state.organization.denied_categories.includes(category),
            );
            const userRequired = group.categories.some((category) =>
              state.organization.user_required_categories.includes(category),
            );
            const value = organizationDenied
              ? "deny"
              : userRequired
                ? "ask_user"
                : effectiveGroupDecision(customRules, group.categories);
            const restricted = organizationDenied || userRequired;
            return (
              <div
                key={group.id}
                className="flex items-center gap-3 border-b border-border/45 py-3 last:border-b-0"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{group.label}</p>
                  <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
                    {restricted
                      ? organizationDenied
                        ? "组织策略已禁止此类操作"
                        : "组织策略要求每次由用户确认"
                      : group.description}
                  </p>
                </div>
                <select
                  value={value}
                  disabled={restricted || unavailable}
                  aria-label={`${group.label}审批方式`}
                  onChange={(event) => {
                    const decision = event.target.value as PermissionDecision;
                    setCustomRules((current) => {
                      const next = { ...current };
                      for (const category of group.categories)
                        next[category] = decision;
                      return next;
                    });
                  }}
                  className="h-9 w-28 shrink-0 rounded-lg border border-input bg-background px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:opacity-55 sm:w-32"
                >
                  {CUSTOM_DECISION_OPTIONS.map((option) => (
                    <option
                      key={option.value}
                      value={option.value}
                      disabled={
                        option.value === "auto_review" &&
                        !state.organization.reviewer_enabled
                      }
                    >
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
        <div className="flex justify-end gap-2 border-t border-border/55 bg-muted/25 px-5 py-4">
          <Dialog.Close render={<Button variant="outline" />}>
            取消
          </Dialog.Close>
          <Button disabled={unavailable} onClick={() => void saveCustom()}>
            保存并启用
          </Button>
        </div>
      </PermissionDialog>
    </>
  );
}
