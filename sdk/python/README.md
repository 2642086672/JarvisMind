# jarvismind-sdk (Python)

A small client for the JarvisMind agent API. It uses only the Python standard library.

```bash
pip install ./sdk/python
```

```python
from jarvismind_sdk import JarvisMindClient

client = JarvisMindClient("http://127.0.0.1:8777", token="jmt_...", space_id="user-42")
receipt = client.remember("我喜欢喝乌龙茶", conversation_id="session-123")
context = client.context("我喜欢喝什么茶？", limit=6)
```

- The token is never part of `repr()` or an error message.
- The SDK does not retry. Errors carry `status`, `code`, `details` and `retry_after`.
- `remember`, `extract` and `forget` return the server's receipt or request. The SDK has no direct write.

Full method list and error classes: [docs/SDK.md](../../docs/SDK.md).

Tests: `cd sdk/python && python -m pytest tests` (they use a local stub server).
