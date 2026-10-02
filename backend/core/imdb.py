"""Small client for reading public IMDb title lists.

IMDb's website uses this unauthenticated GraphQL endpoint for public list
pages.  Scrob only requests the list metadata and title identifiers it needs;
the title metadata itself still comes from the configured TMDB integration.
"""

import httpx


IMDB_GRAPHQL_URL = "https://caching.graphql.imdb.com/"
_HTTP_TIMEOUT = 30.0

_PUBLIC_LIST_QUERY = """
query PublicTitleList($id: ID!, $first: Int!, $after: String) {
  list(id: $id) {
    id
    name { originalText }
    description { originalText { plainText } }
    titleListItemSearch(first: $first, after: $after) {
      edges {
        title {
          id
          titleText { text }
          titleType { id }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


async def get_public_list_page(
    list_id: str,
    *,
    after: str | None = None,
    first: int = 100,
) -> dict:
    """Return one page of a public IMDb title list.

    A private, deleted, or unknown list is returned as an empty ``list`` by
    IMDb. GraphQL errors are surfaced as HTTP-like failures so callers can
    present the same not-found response as the TMDB/TVDB importers.
    """
    payload = {
        "operationName": "PublicTitleList",
        "query": _PUBLIC_LIST_QUERY,
        "variables": {"id": list_id, "first": first, "after": after},
    }
    headers = {
        "Accept": "application/graphql-response+json, application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.imdb.com",
        "User-Agent": "Scrob/1.0 (public IMDb list import)",
        "x-imdb-client-name": "imdb-web-next-localized",
        "x-imdb-user-language": "en-US",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT)) as client:
        response = await client.post(IMDB_GRAPHQL_URL, json=payload, headers=headers)
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError("IMDb returned an invalid response")

    if data.get("errors"):
        message = data["errors"][0].get("message") or "IMDb rejected the list request"
        raise RuntimeError(message)
    response.raise_for_status()
    list_data = (data.get("data") or {}).get("list")
    if not list_data:
        raise LookupError("IMDb list is private or does not exist")
    return list_data
