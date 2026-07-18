/// <reference types="vite/client" />

interface AiJsUnpackRuntimeConfig {
  apiBaseUrl?: string;
  authToken?: string;
  userId?: string;
  projectId?: string;
}

interface Window {
  __AI_JSUNPACK_CONFIG__?: AiJsUnpackRuntimeConfig;
}
