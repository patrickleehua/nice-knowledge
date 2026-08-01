"use client";

import { LoaderCircle, Play, Tags } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { SourceDocument } from "@/lib/types";

export interface DocumentTypeOption {
  value: string;
  label: string;
}

export function DocumentIntakePanel({
  documents,
  typeOptions,
  selected,
  assignments,
  batchType,
  classificationPending,
  enqueuePending,
  onToggle,
  onToggleAll,
  onBatchTypeChange,
  onAssignmentChange,
  onEnqueue,
}: {
  documents: SourceDocument[];
  typeOptions: DocumentTypeOption[];
  selected: Set<string>;
  assignments: Record<string, string>;
  batchType: string | null;
  classificationPending: boolean;
  enqueuePending: boolean;
  onToggle: (documentId: string) => void;
  onToggleAll: (checked: boolean) => void;
  onBatchTypeChange: (docType: string) => void;
  onAssignmentChange: (documentId: string, docType: string) => void;
  onEnqueue: () => void;
}) {
  const selectedDocuments = documents.filter((document) =>
    selected.has(document.id),
  );
  const allSelected =
    documents.length > 0 &&
    documents.every((document) => selected.has(document.id));
  const missingClassification = selectedDocuments.some(
    (document) => !assignments[document.id],
  );

  return (
    <section className="overflow-hidden rounded-md border border-border bg-muted/15">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-2 py-1.5">
        <Checkbox
          checked={allSelected}
          aria-label="全选待处理文件"
          onCheckedChange={(checked) => onToggleAll(checked === true)}
        />
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="text-sm font-medium">
            待处理
            <span className="ml-1.5 text-xs font-normal text-muted-foreground">
              {documents.length}
            </span>
          </h2>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {selectedDocuments.length > 0 && (
            <span className="text-xs text-muted-foreground">
              已选 {selectedDocuments.length}
            </span>
          )}
          <Select
            items={Object.fromEntries(
              typeOptions.map((option) => [option.value, option.label]),
            )}
            value={batchType}
            disabled={
              selectedDocuments.length === 0 ||
              classificationPending ||
              enqueuePending
            }
            onValueChange={(value) => onBatchTypeChange(String(value))}
          >
            <SelectTrigger
              size="sm"
              className="min-w-36"
              aria-label="批量文档类型"
            >
              <Tags className="size-3.5" />
              <SelectValue placeholder="批量设置类型" />
            </SelectTrigger>
            <SelectContent>
              {typeOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            size="xs"
            aria-label="排入解析队列"
            disabled={
              enqueuePending ||
              classificationPending ||
              selectedDocuments.length === 0 ||
              missingClassification
            }
            onClick={onEnqueue}
          >
            {enqueuePending ? (
              <LoaderCircle className="animate-spin" />
            ) : (
              <Play />
            )}
            排入队列
          </Button>
        </div>
      </div>

      <div className="max-h-64 divide-y divide-border overflow-y-auto">
        {documents.map((document) => (
          <div
            key={document.id}
            className="flex items-center gap-2 px-2 py-1.5"
          >
            <Checkbox
              checked={selected.has(document.id)}
              aria-label={`选择待处理文件 ${document.filename}`}
              onCheckedChange={() => onToggle(document.id)}
            />
            <span className="min-w-0 flex-1 truncate text-sm">
              {document.filename}
            </span>
            <Select
              items={Object.fromEntries(
                typeOptions.map((option) => [option.value, option.label]),
              )}
              value={assignments[document.id] || null}
              disabled={classificationPending || enqueuePending}
              onValueChange={(value) =>
                onAssignmentChange(document.id, String(value))
              }
            >
              <SelectTrigger
                size="sm"
                className="w-40"
                aria-label={`设置 ${document.filename} 的文档类型`}
              >
                <SelectValue placeholder="设置类型" />
              </SelectTrigger>
              <SelectContent>
                {typeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ))}
      </div>
    </section>
  );
}
