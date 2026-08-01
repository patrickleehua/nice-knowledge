export const SUPPORTED_DOCUMENT_SUFFIXES = Object.freeze([
  ".docx",
  ".xlsx",
  ".xlsm",
  ".xls",
  ".pptx",
  ".pdf",
  ".csv",
  ".txt",
  ".md",
  ".jpg",
  ".jpeg",
  ".png",
  ".webp",
]);

/**
 * @template {{ name: string, size: number }} T
 * @param {T[]} files
 * @param {number} maxFileBytes
 * @param {number} maxBatchFiles
 */
export function documentUploadPreflight(files, maxFileBytes, maxBatchFiles) {
  const accepted = [];
  let unsupported = 0;
  let empty = 0;
  let oversized = 0;

  for (const file of files) {
    const filename = file.name.toLowerCase();
    if (
      !SUPPORTED_DOCUMENT_SUFFIXES.some((suffix) => filename.endsWith(suffix))
    ) {
      unsupported += 1;
    } else if (file.size === 0) {
      empty += 1;
    } else if (file.size > maxFileBytes) {
      oversized += 1;
    } else {
      accepted.push(file);
    }
  }

  const eligible = accepted.length;
  const batchExceeded = eligible > maxBatchFiles;
  return {
    accepted: batchExceeded ? [] : accepted,
    eligible,
    unsupported,
    empty,
    oversized,
    batchExceeded,
  };
}
