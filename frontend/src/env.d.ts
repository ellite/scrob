/// <reference path="../.astro/types.d.ts" />
/// <reference types="astro/client" />

declare namespace App {
  interface Locals {
    user: {
      id: number;
      username: string;
      display_name: string;
      email: string;
      role: string;
    } | null;
    token: string | undefined;
    settings?: import('./lib/api').UserSettings;
    hasRpdbKey?: boolean;
  }
}

interface Window {
  __HAS_RPDB__: boolean;
  ratingPosterUrl: typeof import('./lib/posters').ratingPosterUrl;
}
