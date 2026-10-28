import { defaultCache } from "@serwist/next/worker";
import {
  NetworkOnly,
  Serwist,
  type PrecacheEntry,
  type SerwistGlobalConfig,
} from "serwist";

declare global {
  interface WorkerGlobalScope extends SerwistGlobalConfig {
    __SW_MANIFEST: (PrecacheEntry | string)[] | undefined;
  }
}

declare const self: ServiceWorkerGlobalScope;

const serwist = new Serwist({
  precacheEntries: self.__SW_MANIFEST,

  skipWaiting: true,
  clientsClaim: true,
  navigationPreload: true,

  runtimeCaching: [
    // --------------------------------------------------
    // NEVER CACHE API REQUESTS
    // --------------------------------------------------
    {
      matcher: ({ url }) => {
        return url.pathname.startsWith("/api/");
      },
      handler: new NetworkOnly(),
    },

    // --------------------------------------------------
    // NEVER CACHE AUTHENTICATED APPLICATION PAGES
    // --------------------------------------------------
    {
      matcher: ({ url }) => {
        return (
          url.pathname.startsWith("/admin") ||
          url.pathname.startsWith("/superadmin") ||
          url.pathname.startsWith("/employee")
        );
      },
      handler: new NetworkOnly(),
    },

    // --------------------------------------------------
    // CACHE NORMAL PUBLIC NEXT.JS ASSETS/PAGES
    // --------------------------------------------------
    ...defaultCache,
  ],
});

serwist.addEventListeners();