import { useConsole } from "../app/ConsoleContext";
import { api } from "../lib/api/endpoints";
import { useAsync } from "../lib/hooks";
import { Badge, Button, Card, CodeBlock, ErrorState, Field, KeyValue, LoadingState, NotAvailable, PageHeader } from "../components/ui";
import type { ThemeName } from "../lib/session";

function timezoneOptions(current: string): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  const zones = intl.supportedValuesOf?.("timeZone") ?? [];
  const unique = zones.includes(current) ? zones : [current, ...zones];
  return unique.slice(0, 600);
}

function isPrimitive(value: unknown): value is string | number | boolean | null {
  return value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

export function SettingsPage() {
  const { spaceId, theme, setTheme, timezone, setTimezone, signOut } = useConsole();
  const settings = useAsync(() => api.settings(), []);
  const info = useAsync(() => api.systemInfo(), []);

  const sections = settings.data ? Object.entries(settings.data.data) : [];

  return (
    <>
      <PageHeader title="设置" description="界面偏好保存在本浏览器中；服务端设置只读展示，修改需要在服务端配置文件中进行。" />

      <div className="grid two">
        <Card title="界面偏好" subtitle="保存在本浏览器，不影响服务端">
          <div className="stack">
            <Field label="主题" id="theme-select">
              <select id="theme-select" className="select" value={theme} onChange={(event) => setTheme(event.target.value as ThemeName)}>
                <option value="dark">深色</option>
                <option value="light">浅色</option>
              </select>
            </Field>
            <Field label="时区" id="timezone-select" hint="所有时间都按这个时区显示">
              <select id="timezone-select" className="select" value={timezone} onChange={(event) => setTimezone(event.target.value)}>
                {timezoneOptions(timezone).map((zone) => (
                  <option key={zone} value={zone}>
                    {zone}
                  </option>
                ))}
              </select>
            </Field>
            <KeyValue items={[{ label: "当前记忆空间", value: <code>{spaceId}</code> }]} />
          </div>
        </Card>

        <Card title="会话" subtitle="所有者令牌只保存在本标签页的 sessionStorage 中">
          <div className="stack">
            <p className="muted small">关闭标签页或点击退出，令牌会立即从浏览器中清除。</p>
            <div>
              <Button variant="danger" onClick={signOut}>
                退出登录
              </Button>
            </div>
          </div>
        </Card>
      </div>

      <Card title="服务端设置（只读）" subtitle="不包含密钥、令牌与数据目录路径">
        {settings.error ? (
          <ErrorState error={settings.error} onRetry={settings.reload} />
        ) : !settings.data ? (
          <LoadingState label="正在读取设置…" />
        ) : (
          <div className="grid two">
            {sections.map(([section, values]) => (
              <Card key={section} title={section}>
                {values && typeof values === "object" && !Array.isArray(values) ? (
                  <KeyValue
                    items={Object.entries(values as Record<string, unknown>).map(([key, value]) => ({
                      label: key,
                      value: isPrimitive(value) ? String(value) : <CodeBlock value={value} />,
                    }))}
                  />
                ) : (
                  <CodeBlock value={values} />
                )}
              </Card>
            ))}
          </div>
        )}
      </Card>

      <Card title="关于 JarvisMind">
        {info.error ? (
          <ErrorState error={info.error} onRetry={info.reload} />
        ) : info.data ? (
          <KeyValue
            items={[
              { label: "产品", value: info.data.name },
              { label: "版本", value: <Badge tone="info">{info.data.version}</Badge> },
              { label: "Python", value: info.data.python_version },
              { label: "数据库", value: info.data.database_type },
              { label: "构建号", value: <NotAvailable reason="构建流程未注入构建号" /> },
              { label: "发布说明", value: "CHANGELOG.md" },
              { label: "文档", value: "docs/README.md" },
              { label: "许可证", value: "MIT（见仓库根目录 LICENSE）" },
            ]}
          />
        ) : (
          <LoadingState label="正在读取版本…" />
        )}
      </Card>
    </>
  );
}
