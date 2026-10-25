/** 「思考 + 工具调用」过程块的数据形状：ChatView 的脚本化演示与 ReaderChat 的真实 SSE 共用。 */

export type ChainStepStatus = 'running' | 'ok' | 'bad';

export type ChainStep = {
  id: string;
  /** think = 模型推理；tool = 工具调用（检索 / 比对 / 读文件…） */
  kind: 'think' | 'tool';
  label: string;
  text?: string;
  status: ChainStepStatus;
  ms?: number;
};
