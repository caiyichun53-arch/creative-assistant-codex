"""飞书推送薄通道(固定函数):直接走 Feishu OpenAPI。

数据真相源在本地 SQLite,飞书只是视图/遥控器。
凭证和 chat_id 从本项目 .env 读,不依赖 lark-cli/global profile。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "feishu"))
from client import api, env  # noqa: E402

CHAT_ID = env("FEISHU_CHAT_ID")


def send_text(text: str, chat_id: str | None = None) -> None:
    api("POST", "/open-apis/im/v1/messages",
        {"receive_id_type": "chat_id"},
        {"receive_id": chat_id or CHAT_ID, "msg_type": "text",
         "content": json.dumps({"text": text}, ensure_ascii=False)})


def send_card(header: str, md_blocks: list[str], note: str | None = None,
              chat_id: str | None = None, template: str = "blue") -> None:
    """发交互卡片:header 标题,md_blocks 每项一个 markdown 块(块间分隔线)。"""
    elements = []
    for i, md in enumerate(md_blocks):
        if i:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": md})
    if note:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    card = {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": header}, "template": template},
            "elements": elements}
    api("POST", "/open-apis/im/v1/messages",
        {"receive_id_type": "chat_id"},
        {"receive_id": chat_id or CHAT_ID, "msg_type": "interactive",
         "content": json.dumps(card, ensure_ascii=False)})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    send_text("飞书推送薄通道自检 OK")
    print("OK")
