import os
import logging
from flask import Flask, request, jsonify
from notion_client import Client as NotionClient
import anthropic
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Environment variables ──────────────────────────────────────────────────────
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
TODOIST_API_TOKEN  = os.environ.get("TODOIST_API_TOKEN", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")

# ── Todoist project IDs ────────────────────────────────────────────────────────
TODOIST_PROJECTS = {
    "Costa Vida": "6gxCWvg69HqV4VMX",
    "FatCats":    "6gxCWvgmVvXxVfcM",
    "Work":       "6gx9wqFRmFPcCJ6v",
    "Personal":   "6gx9wqFmmQ9xCqVw",
}

notion           = NotionClient(auth=NOTION_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Helpers ────────────────────────────────────────────────────────────────────

def classify_project(task_text: str) -> str:
    """Ask Claude Haiku which Todoist project a task belongs to."""
    message = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify this task into exactly one of these projects: "
                    "Costa Vida, FatCats, Work, Personal.\n\n"
                    "Context:\n"
                    "- Costa Vida: tasks related to the Costa Vida restaurant\n"
                    "- FatCats: tasks related to the FatCats entertainment venue\n"
                    "- Work: professional tasks not specific to either business\n"
                    "- Personal: personal errands, home, family, health\n\n"
                    f"Task: {task_text}\n\n"
                    "Reply with ONLY the project name."
                ),
            }
        ],
    )
    result = message.content[0].text.strip()
    return result if result in TODOIST_PROJECTS else "Personal"


def get_checked_todo_blocks(page_id: str) -> list[str]:
    """
    Fetch all blocks on a Notion page and return the plain text of every
    to_do block that is checked (checked == True).
    Handles pagination so long pages are fully scanned.
    """
    checked_items = []
    cursor = None

    while True:
        kwargs = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor

        response = notion.blocks.children.list(**kwargs)

        for block in response.get("results", []):
            if block.get("type") != "to_do":
                continue
            todo = block.get("to_do", {})
            if not todo.get("checked", False):
                continue
            rich_text = todo.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rich_text).strip()
            if text:
                checked_items.append({"block_id": block["id"], "text": text})

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return checked_items


def uncheck_block(block_id: str) -> None:
    """Reset a to_do block to unchecked after it's been sent to Todoist."""
    notion.blocks.update(
        block_id=block_id,
        to_do={"checked": False},
    )


def create_todoist_task(task_name: str, project_id: str) -> dict:
    """Create a task in Todoist."""
    resp = requests.post(
        "https://api.todoist.com/rest/v2/tasks",
        headers={
            "Authorization": f"Bearer {TODOIST_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"content": task_name, "project_id": project_id},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/debug")
def debug():
    """Shows which required env vars are present (not their values)."""
    return jsonify({
        "NOTION_TOKEN":      "SET" if NOTION_TOKEN else "MISSING",
        "TODOIST_API_TOKEN": "SET" if TODOIST_API_TOKEN else "MISSING",
        "ANTHROPIC_API_KEY": "SET" if ANTHROPIC_API_KEY else "MISSING",
        "WEBHOOK_SECRET":    "SET" if WEBHOOK_SECRET else "MISSING",
    })


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    # Optional shared-secret check
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    # The Notion button sends the page ID in the request body
    body = request.get_json(silent=True) or {}
    page_id = body.get("page_id") or body.get("data", {}).get("page_id")

    if not page_id:
        logger.error("No page_id in request body")
        return jsonify({"error": "page_id required in request body"}), 400

    logger.info(f"Webhook received for page: {page_id}")

    try:
        checked_items = get_checked_todo_blocks(page_id)
        logger.info(f"Found {len(checked_items)} checked item(s)")

        if not checked_items:
            return jsonify({
                "status": "success",
                "message": "No checked items found",
                "tasks_created": 0,
            })

        created, errors = [], []

        for item in checked_items:
            try:
                project_name = classify_project(item["text"])
                project_id   = TODOIST_PROJECTS[project_name]
                logger.info(f"Creating: '{item['text']}' → {project_name}")

                todoist_task = create_todoist_task(item["text"], project_id)
                created.append({
                    "task":       item["text"],
                    "project":    project_name,
                    "todoist_id": todoist_task.get("id"),
                })
                uncheck_block(item["block_id"])

            except Exception as exc:
                logger.error(f"Error on '{item['text']}': {exc}")
                errors.append({"text": item["text"], "error": str(exc)})

        return jsonify({
            "status":        "success",
            "tasks_created": len(created),
            "tasks":         created,
            "errors":        errors,
        })

    except Exception as exc:
        logger.error(f"Webhook error: {exc}")
        return jsonify({"error": str(exc)}), 500


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
