import { useState, type FormEvent } from "react";
import { describeError } from "../lib/api/client";
import { api } from "../lib/api/endpoints";
import { setOwnerToken } from "../lib/session";
import { Button, Field, Notice, PageHeader } from "../components/ui";

export function LoginPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const candidate = token.trim();
    if (candidate.length < 8) {
      setError("请输入所有者令牌。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.validateOwnerToken(candidate);
      setOwnerToken(candidate);
      setToken("");
      onSignedIn();
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="login-card">
        <div className="brand brand-large">
          <span className="brand-mark" aria-hidden="true">
            J
          </span>
          <div>
            <strong>JarvisMind Console</strong>
            <span className="brand-sub">我要给她完整的一生</span>
          </div>
        </div>
        <PageHeader
          title="使用所有者令牌登录"
          description="这是本机安装的所有者凭据：环境变量 JARVISMIND_API_TOKEN，或数据目录中的 .jarvismind_token。"
        />
        <form onSubmit={handleSubmit} className="stack">
          <Field label="所有者令牌" id="owner-token" hint="令牌只保存在本浏览器标签页的 sessionStorage 中，关闭标签页即失效。">
            <input
              id="owner-token"
              className="input"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
          </Field>
          {error ? <Notice tone="danger">{error}</Notice> : null}
          <Button type="submit" variant="primary" loading={busy}>
            验证并进入
          </Button>
        </form>
        <Notice tone="info" title="Agent 令牌不能登录控制台">
          Agent 令牌只用于 /api/v1/agent 接口。控制台只接受所有者凭据，服务端会拒绝其它凭据。
        </Notice>
      </div>
    </div>
  );
}
