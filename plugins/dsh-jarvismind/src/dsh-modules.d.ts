/**
 * Build-time shims for the packages the DSH Desktop runtime resolves from its
 * own installation. The runtime never ships `.d.ts` files for these packages,
 * so the plugin keeps a minimal structural declaration of exactly the surface
 * it uses. At execution time the real implementations are used.
 */

declare module '@deepseek-ai/schemastery' {
  export interface Schema<T = unknown> {
    default(value: T): Schema<T>
  }

  const z: {
    string(): Schema<string>
    number(): Schema<number>
    boolean(): Schema<boolean>
    object(shape: Record<string, Schema<any>>): Schema<Record<string, any>>
  }

  export default z
}

declare module '@deepseek-ai/dsh-tools' {
  export interface ToolRenderBlock {
    type: 'text'
    text: string
  }

  export interface ToolOutputDefinition {
    schema: unknown
    render(args: unknown, value: unknown): ToolRenderBlock[]
  }

  export interface ToolParameterSpec {
    type: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object' | 'null' | 'json'
    required?: boolean
    description?: string
    items?: ToolParameterSpec
    properties?: Record<string, ToolParameterSpec>
    [key: string]: unknown
  }

  export interface ToolDefinition {
    name: string
    description: string
    parameters: unknown
    output: ToolOutputDefinition
    execute(args: any, exec: any): Promise<unknown>
    [key: string]: unknown
  }

  export interface DefineToolOptions<Args> {
    name: string
    description: string
    parameters: Record<string, ToolParameterSpec>
    output: ToolOutputDefinition
    execute(args: Args, exec: any): Promise<unknown>
    timeoutMs?: number
    deferLoading?: boolean
  }

  export function defineTool<Args>(options: DefineToolOptions<Args>): ToolDefinition
}