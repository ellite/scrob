import type { APIRoute } from "astro";
import { proxyBackendPath } from "../lib/backend-proxy";

export const GET: APIRoute = (ctx) => proxyBackendPath("/openapi.json", ctx.locals.token);
