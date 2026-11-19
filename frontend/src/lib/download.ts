/** The file name the server suggests in Content-Disposition, reduced to safe characters. */
export function filenameFrom(response: Response, fallback: string): string {
  const header = response.headers.get("Content-Disposition") ?? "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
  const name = match?.[1]?.replace(/[^A-Za-z0-9._-]/g, "_");
  return name ? name : fallback;
}

const REVOKE_AFTER_MS = 1000;

/** Hand a Blob to the browser as a download through a temporary object URL (revoked after a second, so slow browsers have started the download). */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS);
}
