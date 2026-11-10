export const PAGE_SIZES = [20, 30, 40, 50, 100];

export function resolvePageSize(requested, fallback, maximum = 100) {
  const size = Number(requested);
  return Math.min(PAGE_SIZES.includes(size) ? size : fallback, maximum);
}

export function pageSizeUrl(currentUrl, size) {
  const url = new URL(currentUrl);
  url.searchParams.set("page_size", String(size));
  url.searchParams.delete("page");
  return url.href;
}
