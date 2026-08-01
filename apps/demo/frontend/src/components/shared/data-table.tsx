/**
 * 全站唯一表格实现,基于 @tanstack/react-table + ui/table 原语。
 * 内建:列排序 / 行选择 / 行点击 / 加载骨架 / 错误态(ErrorState)/ 空态(EmptyState)/ 分页条。
 *
 * @example
 * ```tsx
 * const columns: ColumnDef<Project, any>[] = [
 *   { accessorKey: "name", header: "旅行计划名", enableSorting: true },
 *   { accessorKey: "status", header: "状态", enableSorting: false,
 *     cell: ({ row }) => <StatusBadge meta={PROJECT_STATUS[row.original.status]} /> },
 * ];
 *
 * <DataTable
 *   columns={columns}
 *   data={query.data?.items}
 *   isLoading={query.isLoading}
 *   error={query.error}
 *   onRetry={() => query.refetch()}
 *   empty={{ icon: FolderOpen, title: "暂无旅行计划", action: <Button>新建旅行计划</Button> }}
 *   onRowClick={(row) => router.push(`/app/projects/${row.id}`)}
 *   getRowId={(row) => row.id}
 *   pagination={{ page, totalPages, onPageChange: setPage }}
 * />
 * ```
 */
"use client";

import * as React from "react";
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type RowSelectionState,
  type SortingState,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown, type LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/shared/empty-state";
import { ErrorState } from "@/components/shared/error-state";
import { cn } from "@/lib/utils";

const SKELETON_ROWS = 5;

export interface DataTableProps<TData> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  columns: ColumnDef<TData, any>[];
  data: TData[] | undefined;
  isLoading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  empty?: {
    icon?: LucideIcon;
    title: string;
    description?: string;
    action?: React.ReactNode;
  };
  onRowClick?: (row: TData) => void;
  getRowId?: (row: TData) => string;
  enableRowSelection?: boolean;
  onSelectionChange?: (rows: TData[]) => void;
  pagination?: {
    page: number;
    totalPages: number;
    onPageChange: (p: number) => void;
  };
}

export function DataTable<TData>({
  columns,
  data,
  isLoading,
  error,
  onRetry,
  empty,
  onRowClick,
  getRowId,
  enableRowSelection,
  onSelectionChange,
  pagination,
}: DataTableProps<TData>) {
  const [sorting, setSorting] = React.useState<SortingState>([]);
  const [rowSelection, setRowSelection] = React.useState<RowSelectionState>(
    {},
  );

  const tableData = React.useMemo(() => data ?? [], [data]);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const allColumns = React.useMemo<ColumnDef<TData, any>[]>(() => {
    if (!enableRowSelection) return columns;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const selectColumn: ColumnDef<TData, any> = {
      id: "__select__",
      enableSorting: false,
      header: ({ table }) => (
        <Checkbox
          aria-label="全选"
          checked={table.getIsAllRowsSelected()}
          onCheckedChange={(checked) => table.toggleAllRowsSelected(checked)}
        />
      ),
      cell: ({ row }) => (
        <Checkbox
          aria-label="选择行"
          checked={row.getIsSelected()}
          disabled={!row.getCanSelect()}
          onCheckedChange={(checked) => row.toggleSelected(checked)}
          onClick={(event) => event.stopPropagation()}
        />
      ),
    };
    return [selectColumn, ...columns];
  }, [columns, enableRowSelection]);

  const table = useReactTable({
    data: tableData,
    columns: allColumns,
    state: { sorting, rowSelection },
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    enableRowSelection: !!enableRowSelection,
    getRowId: getRowId ? (originalRow) => getRowId(originalRow) : undefined,
  });

  // 选中集变化时通知外部(ref 避免回调身份变化触发多余通知)
  const selectionCallbackRef = React.useRef(onSelectionChange);
  React.useEffect(() => {
    selectionCallbackRef.current = onSelectionChange;
  });
  React.useEffect(() => {
    selectionCallbackRef.current?.(
      table.getSelectedRowModel().rows.map((row) => row.original),
    );
  }, [rowSelection, table]);

  const rows = table.getRowModel().rows;
  const showEmpty = !isLoading && !error && rows.length === 0;

  let content: React.ReactNode;
  if (error) {
    content = <ErrorState error={error} onRetry={onRetry} />;
  } else if (showEmpty) {
    content = (
      <EmptyState
        icon={empty?.icon}
        title={empty?.title ?? "暂无数据"}
        description={empty?.description}
        action={empty?.action}
      />
    );
  } else {
    content = (
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id} className="hover:bg-transparent">
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id}>
                  {header.isPlaceholder ? null : header.column.getCanSort() ? (
                    <button
                      type="button"
                      className="-ml-1 inline-flex items-center gap-1 rounded px-1 py-0.5 transition-colors outline-none select-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
                      onClick={header.column.getToggleSortingHandler()}
                    >
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                      {header.column.getIsSorted() === "asc" ? (
                        <ArrowUp className="size-3.5" />
                      ) : header.column.getIsSorted() === "desc" ? (
                        <ArrowDown className="size-3.5" />
                      ) : (
                        <ArrowUpDown className="size-3.5 text-muted-foreground/60" />
                      )}
                    </button>
                  ) : (
                    flexRender(
                      header.column.columnDef.header,
                      header.getContext(),
                    )
                  )}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {isLoading
            ? Array.from({ length: SKELETON_ROWS }, (_, index) => (
                <TableRow
                  key={`skeleton-${index}`}
                  className="hover:bg-transparent"
                >
                  {table.getAllLeafColumns().map((column) => (
                    <TableCell key={column.id}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            : rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-state={row.getIsSelected() ? "selected" : undefined}
                  className={cn(
                    "hover:bg-accent/50",
                    onRowClick && "cursor-pointer",
                  )}
                  onClick={
                    onRowClick ? () => onRowClick(row.original) : undefined
                  }
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
        </TableBody>
      </Table>
    );
  }

  return (
    <div className="space-y-3">
      {content}
      {pagination && !error && (
        <div className="flex items-center justify-end gap-2">
          <span className="text-sm text-muted-foreground tabular-nums">
            第 {pagination.page} / {Math.max(pagination.totalPages, 1)} 页
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={isLoading || pagination.page <= 1}
            onClick={() => pagination.onPageChange(pagination.page - 1)}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={isLoading || pagination.page >= pagination.totalPages}
            onClick={() => pagination.onPageChange(pagination.page + 1)}
          >
            下一页
          </Button>
        </div>
      )}
    </div>
  );
}
