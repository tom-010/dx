/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Absolute API origin for native builds; unset (same origin) in the web build. */
  readonly VITE_API_URL?: string;
}
