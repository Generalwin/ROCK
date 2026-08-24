"""E2B SDK demo for ROCK sandbox.

Use the E2B SDK to create/destroy sandboxes via ROCK's E2B-compatible control plane.
Use the ROCK SDK for command execution (data plane).
"""

import asyncio
import os

from e2b import AsyncSandbox
from rock.actions import CreateBashSessionRequest
from rock.sdk.sandbox.client import Sandbox
from rock.sdk.sandbox.config import SandboxConfig

ROCK_ADMIN_URL = os.environ.get("ROCK_ADMIN_URL", "http://localhost:8080")
os.environ.setdefault("E2B_API_URL", ROCK_ADMIN_URL)


async def main():
    # 控制面：E2B SDK 创建 sandbox
    e2b_sandbox = await AsyncSandbox.create(
        template=os.environ.get("E2B_TEMPLATE_ID", "pool-sample"),
        api_key=os.environ.get("E2B_API_KEY", "e2b_0000000000000000000000000000000000000000"),
    )
    print(f"Sandbox created: {e2b_sandbox.sandbox_id}")

    try:
        # 数据面：ROCK SDK 连接并执行命令
        rock_sandbox = Sandbox(SandboxConfig())
        await rock_sandbox.attach(e2b_sandbox.sandbox_id)

        await rock_sandbox.create_session(CreateBashSessionRequest(session="bash-1"))
        result = await rock_sandbox.arun(cmd="echo Hello ROCK", session="bash-1")
        print("\n" + "*" * 50 + "\n" + result.output + "\n" + "*" * 50 + "\n")

    finally:
        await e2b_sandbox.kill()
        print("Sandbox killed.")


if __name__ == "__main__":
    asyncio.run(main())
