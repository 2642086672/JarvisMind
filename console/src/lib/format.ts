export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export function formatDateTime(value: string | null | undefined, timezone: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: timezone,
    }).format(date);
  } catch {
    return date.toISOString();
  }
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const whole = Math.floor(seconds);
  if (whole < 60) return `${whole} 秒`;
  if (whole < 3600) return `${Math.floor(whole / 60)} 分 ${whole % 60} 秒`;
  const hours = Math.floor(whole / 3600);
  return `${hours} 小时 ${Math.floor((whole % 3600) / 60)} 分`;
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

export function percent(part: number, total: number): number {
  return total > 0 ? Math.round((part / total) * 100) : 0;
}

const MEMORY_STATUS_LABELS: Record<string, string> = {
  ACTIVE: "有效",
  ARCHIVED: "已归档",
  DELETED: "已遗忘",
  MERGED: "已合并",
};

const MEMORY_TYPE_LABELS: Record<string, string> = {
  PROFILE: "用户画像",
  SEMANTIC: "知识事实",
  EPISODIC: "经历事件",
  PROJECT: "项目",
  WORKING: "工作记忆",
  REFLECTION: "反思总结",
};

const FORGET_STATUS_LABELS: Record<string, string> = {
  PENDING: "待确认",
  APPROVED: "已批准",
  EXECUTED: "已执行",
  REJECTED: "已拒绝",
  FAILED: "执行失败",
};

export function memoryStatusLabel(status: string): string {
  return MEMORY_STATUS_LABELS[status] ?? status;
}

export function memoryTypeLabel(type: string): string {
  return MEMORY_TYPE_LABELS[type] ?? type;
}

export function forgetStatusLabel(status: string): string {
  return FORGET_STATUS_LABELS[status] ?? status;
}

export function statusTone(status: string): Tone {
  switch (status) {
    case "ACTIVE":
    case "OK":
    case "ok":
    case "ready":
    case "APPROVED":
    case "EXECUTED":
    case "SAVED":
    case "UPDATED":
    case "active":
      return "success";
    case "ARCHIVED":
    case "PENDING":
    case "PENDING_AUTHORIZATION":
    case "PENDING_REVIEW":
    case "unavailable":
    case "disabled":
    case "schema_not_ready":
      return "warning";
    case "DELETED":
    case "REJECTED":
    case "FAILED":
    case "error":
    case "revoked":
    case "deleted":
      return "danger";
    case "MERGED":
    case "not_available":
      return "info";
    default:
      return "neutral";
  }
}
