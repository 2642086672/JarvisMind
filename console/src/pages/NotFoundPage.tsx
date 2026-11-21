import { Link } from "react-router-dom";
import { EmptyState } from "../components/ui";

export function NotFoundPage() {
  return (
    <EmptyState
      title="页面不存在"
      description="这个地址没有对应的控制台页面。"
      action={<Link to="/">返回仪表盘</Link>}
    />
  );
}
