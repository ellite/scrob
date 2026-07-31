import type { APIRoute } from "astro";
import { proxyBackendPath } from "../lib/backend-proxy";

export const GET: APIRoute = (ctx) => proxyBackendPath("/redoc", ctx.locals.token);
