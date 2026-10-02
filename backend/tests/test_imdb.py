import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import imdb


class ImdbPublicListClientTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _client_context(response: MagicMock) -> tuple[MagicMock, AsyncMock]:
        client = AsyncMock()
        client.post.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        return context, client

    async def test_uses_string_cursor_required_by_imdb_schema(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "data": {
                "list": {
                    "id": "ls000024621",
                    "titleListItemSearch": {
                        "edges": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            },
        }
        context, client = self._client_context(response)

        with patch("core.imdb.httpx.AsyncClient", return_value=context):
            result = await imdb.get_public_list_page("ls000024621", after="cursor", first=2)

        self.assertEqual(result["id"], "ls000024621")
        payload = client.post.await_args.kwargs["json"]
        self.assertIn("$after: String", payload["query"])
        self.assertEqual(payload["variables"]["after"], "cursor")
        response.raise_for_status.assert_called_once_with()

    async def test_surfaces_graphql_error_from_http_400_response(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "errors": [{"message": "Variable type does not match the schema"}],
        }
        context, _client = self._client_context(response)

        with patch("core.imdb.httpx.AsyncClient", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "Variable type does not match"):
                await imdb.get_public_list_page("ls000024621")

        response.raise_for_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
