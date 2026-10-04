// @ts-check
import { defineConfig } from 'astro/config';

import tailwindcss from '@tailwindcss/vite';

import node from '@astrojs/node';

import { paraglideVitePlugin } from '@inlang/paraglide-js';

// https://astro.build/config
export default defineConfig({
  output: 'server',

  security: {
    checkOrigin: false,
  },

  server: {
    port: 7330,
  },

  vite: {
    plugins: [
      tailwindcss(),
      // UI translations. The language is a user preference, not part of the URL.
      // Resolution order: the signed-in user's account preference
      // (user_settings.ui_language, injected by the custom-account strategy in
      // src/lib/ui-locale.ts), then the ui_language cookie - which is also where
      // client-side <script>s read it from, and where a signed-out visitor's
      // pick lands - then the browser's Accept-Language, then English.
      paraglideVitePlugin({
        project: './project.inlang',
        outdir: './src/paraglide',
        strategy: ['custom-account', 'cookie', 'preferredLanguage', 'baseLocale'],
        cookieName: 'ui_language',
      }),
    ],
    build: {
      rollupOptions: {
        // A misspelled message key (m.nav_hom()) only makes Rollup warn that
        // "nav_hom" is not exported by src/paraglide/messages/_index.js: the
        // build passes, then the page throws "m.nav_hom is not a function" when
        // it renders. Nothing type-checks this project, so fail the build here.
        onwarn(warning, warn) {
          if (warning.code === 'MISSING_EXPORT' && /[\\/]paraglide[\\/]/.test(warning.exporter ?? '')) {
            throw new Error(`Unknown message key: ${warning.message}`);
          }
          warn(warning);
        },
      },
    },
    server: {
      allowedHosts: ['abstract-dev.bellamylab.com', 'scrob-dev.bellamylab.com'],
    }
  },

  adapter: node({
    mode: 'standalone'
  })
});