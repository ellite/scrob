export function mediaNavHref(type, collection = false) {
  return collection ? `/collection/${type}` : `/${type}`;
}
