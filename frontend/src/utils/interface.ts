import type { OutputItem } from "./response";

/** 代码单元格类型 */
export interface CodeCell {
	type: "code";
	content: string;
}

/** 结果单元格类型 */
export interface ResultCell {
	type: "result";
	code_results: OutputItem[];
}

/** 笔记本单元格类型（代码或结果） */
export type NoteCell = CodeCell | ResultCell;

/** 模型配置 */
export interface ModelConfig {
	apiKey: string;
	baseUrl: string;
	modelId: string;
	apiType: string;
	/** 上下文窗口大小（token），用于记忆压缩阈值 */
	contextWindow?: number;
}

/** 历史任务状态（与后端 TaskStatus 一致） */
export type TaskStatus =
	| "running"
	| "completed"
	| "failed"
	| "cancelled"
	| "unknown";

/** 历史任务摘要 */
export interface TaskSummary {
	task_id: string;
	updated_at: string;
	message_count: number;
	has_work_dir: boolean;
	has_res_md: boolean;
	status: TaskStatus;
}

/** 任务列表响应 */
export interface TaskListResponse {
	tasks: TaskSummary[];
}
