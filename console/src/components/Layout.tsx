import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { describeError } from "../lib/api/client";
import { api } from "../lib/api/endpoints";
import { useAsync } from "../lib/hooks";
import { useConsole } from "../app/ConsoleContext";
import { Button } from "./ui";

interface NavItem {
  to: string;
  label: string;
  glyph: string;
}

const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  { label: "总览", items: [{ to: "/", label: "仪表盘", glyph: "◎" }] },
  {
    label: "记忆",
    items: [
      { to: "/memories", label: "记忆浏览器", glyph: "▤" },
      { to: "/context-lab", label: "上下文实验室", glyph: "◇" },
    ],
  },
  { label: "知识", items: [{ to: "/graph", label: "知识图谱", glyph: "⬡" }] },
  { label: "治理", items: [{ to: "/governance", label: "治理中心", glyph: "◈" }] },
  {
    label: "空间与 Agent",
    items: [
      { to: "/spaces", label: "记忆空间", glyph: "▢" },
      { to: "/agents", label: "Agent 管理", glyph: "◉" },
    ],
  },
  { label: "运维", items: [{ to: "/operations", label: "系统运维", glyph: "▥" }] },
  {
    label: "集成",
    items: [
      { to: "/api", label: "API 管理", glyph: "⌘" },
      { to: "/integrations", label: "SDK 与接入", glyph: "⇄" },
    ],
  },
  { label: "数据", items: [{ to: "/backup", label: "备份与导出", glyph: "⛁" }] },
  { label: "系统", items: [{ to: "/settings", label: "设置", glyph: "⚙" }] },
];

export function Layout({ children }: { children: ReactNode }) {
  const { spaceId, setSpaceId, spacesVersion, theme, setTheme, signOut } = useConsole();
  const [navOpen, setNavOpen] = useState(false);
  const spaces = useAsync(() => api.listSpaces(false), [spacesVersion]);
  const activeSpaces = spaces.data?.data ?? [];

  useEffect(() => {
    if (spaces.data && !activeSpaces.some((space) => space.id === spaceId)) {
      setSpaceId("default");
    }
    // Only the loaded list and the selection matter here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spaces.data, spaceId]);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        跳到主要内容
      </a>
      <aside className={navOpen ? "sidebar sidebar-open" : "sidebar"} aria-label="主导航">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            J
          </span>
          <div>
            <strong>JarvisMind</strong>
            <span className="brand-sub">我要给她完整的一生</span>
          </div>
        </div>
        <nav id="sidebar-nav">
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="nav-group">
              <p className="nav-group-label">{group.label}</p>
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) => (isActive ? "nav-link nav-link-active" : "nav-link")}
                  onClick={() => setNavOpen(false)}
                >
                  <span className="nav-glyph" aria-hidden="true">
                    {item.glyph}
                  </span>
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <button
            type="button"
            className="icon-button nav-toggle"
            aria-expanded={navOpen}
            aria-controls="sidebar-nav"
            onClick={() => setNavOpen((open) => !open)}
          >
            菜单
          </button>
          <label className="space-picker">
            <span className="sr-only">记忆空间</span>
            <select
              className="select"
              value={spaceId}
              onChange={(event) => setSpaceId(event.target.value)}
              disabled={spaces.loading && activeSpaces.length === 0}
            >
              {activeSpaces.length === 0 ? <option value={spaceId}>{spaceId}</option> : null}
              {activeSpaces.map((space) => (
                <option key={space.id} value={space.id}>
                  {space.name}（{space.id}）
                </option>
              ))}
            </select>
          </label>
          <div className="topbar-spacer" />
          <Button variant="ghost" size="sm" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
            {theme === "dark" ? "浅色主题" : "深色主题"}
          </Button>
          <Button variant="ghost" size="sm" onClick={signOut}>
            退出
          </Button>
        </header>
        {spaces.error ? (
          <div className="banner" role="alert">
            无法读取记忆空间：{describeError(spaces.error)}
          </div>
        ) : null}
        <main id="main" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
