/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue';
  const component: DefineComponent<{}, {}, any>;
  export default component;
}

declare module 'marked-katex-extension' {
  import type { MarkedExtension } from 'marked';
  export interface KatexOptions {
    throwOnError?: boolean;
    nonStandard?: boolean;
    output?: 'html' | 'mathml' | 'htmlAndMathml';
    errorColor?: string;
    displayMode?: boolean;
    [key: string]: unknown;
  }
  export default function markedKatex(options?: KatexOptions): MarkedExtension;
}
