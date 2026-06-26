import os
import datetime
import logging
from flask import Flask, request, jsonify
from notion_client import Client as NotionClient
import anthropic
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Environment variables ──────────────────────────────────────────────────────
NOTION_TOKEN        = os.environ.get("NOTION_TOKEN", "")
TODOIST_API_TOKEN   = os.environ.get("TODOIST_API_TOKEN", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET", "")
NOTION_DATABASE_ID  = os.environ.get("NOTION_DATABASE_ID", "")

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

def get_todays_digest_page_id() -> str | None:
    """Find the most recently created digest page for today."""
    today = datetime.date.today().isoformat()
    response = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"property": "Date", "date": {"equals": today}},
        sorts=[{"timestamp": "created_time", "direction": "descending"}],
    )
    results = response.get("results", [])
    if not results:
        return None
    return results[0]["id"]


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


def get_checked_todo_blocks(page_id: str) -> list:
    """Return all checked to-do blocks on a Notion page."""
    all_blocks = notion.blocks.children.list(block_id=page_id, page_size=100).get("results", [])
    checked_items = []
    for block in all_blocks:
        if block.get("type") != "to_do":
            continue
        todo = block.get("to_do", {})
        if not todo.get("checked"):
            continue
        rich_text = todo.get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich_text).strip()
        if text:
            checked_items.append({"block_id": block["id"], "text": text})
    return checked_items


def uncheck_block(block_id: str) -> None:
    notion.blocks.update(block_id=block_id, to_do={"checked": False})


def create_todoist_task(task_name: str, project_id: str) -> dict:
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


def process_digest(page_id: str) -> dict:
    """Core logic: read checked items, classify, create Todoist tasks."""
    checked_items = get_checked_todo_blocks(page_id)

    if not checked_items:
        return {"tasks_created": 0, "tasks": [], "errors": [], "message": "No checked items found."}

    created, errors = [], []

    for item in checked_items:
        try:
            project_name = classify_project(item["text"])
            project_id   = TODOIST_PROJECTS[project_name]
            logger.info(f"Creating: '{item['text']}' → {project_name}")
            todoist_task = create_todoist_task(item["text"], project_id)
            created.append({"task": item["text"], "project": project_name})
            uncheck_block(item["block_id"])
        except Exception as exc:
            logger.error(f"Error on '{item['text']}': {exc}")
            errors.append({"text": item["text"], "error": str(exc)})

    return {"tasks_created": len(created), "tasks": created, "errors": errors}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/debug-page")
def debug_page():
    """Show all block types on today's most recent digest page."""
    page_id = get_todays_digest_page_id()
    if not page_id:
        return jsonify({"error": "No page found for today"})

    response = notion.blocks.children.list(block_id=page_id, page_size=100)
    blocks = []
    for block in response.get("results", []):
        btype = block.get("type")
        entry = {"type": btype}
        if btype == "to_do":
            todo = block.get("to_do", {})
            entry["checked"] = todo.get("checked")
            entry["text"] = "".join(t.get("plain_text","") for t in todo.get("rich_text",[]))
        blocks.append(entry)

    return jsonify({"page_id": page_id, "block_count": len(blocks), "blocks": blocks})


@app.route("/trigger")
def trigger():
    """
    Browser-friendly GET endpoint.
    Notion button uses 'Open URL' to visit this page.
    Finds today's digest automatically, processes checked items,
    and returns a readable HTML confirmation.
    """
    try:
        if not NOTION_DATABASE_ID:
            return _html_page("Setup needed", "NOTION_DATABASE_ID environment variable is not set.")

        page_id = get_todays_digest_page_id()
        if not page_id:
            return _html_page("No digest found", "No morning digest page found for today. Run the email digest script first.")

        result = process_digest(page_id)

        if result["tasks_created"] == 0:
            # Debug: count all to_do blocks and how many are checked
            all_blocks = notion.blocks.children.list(block_id=page_id, page_size=100).get("results", [])
            todo_blocks = [b for b in all_blocks if b.get("type") == "to_do"]
            checked_blocks = [b for b in todo_blocks if b.get("to_do", {}).get("checked")]
            debug_info = (
                f"Page ID: {page_id}<br>"
                f"Total blocks: {len(all_blocks)}<br>"
                f"To-do blocks: {len(todo_blocks)}<br>"
                f"Checked to-do blocks: {len(checked_blocks)}"
            )
            return _html_page(
                "Nothing to do",
                f"No checked items found.<br><br>{debug_info}<br><br>"
                "Go back to Notion, check the action items you want, then click the button again."
            )

        task_lines = "".join(
            f"<li><strong>{t['task']}</strong> &rarr; {t['project']}</li>"
            for t in result["tasks"]
        )
        error_lines = ""
        if result["errors"]:
            error_lines = "<p style='color:red'>Errors: " + ", ".join(e["text"] for e in result["errors"]) + "</p>"

        return _html_page(
            f"✅ {result['tasks_created']} task(s) created!",
            f"<ul>{task_lines}</ul>{error_lines}<p>Checkboxes have been reset in Notion.</p>"
        )

    except Exception as exc:
        logger.error(f"Trigger error: {exc}")
        return _html_page("Error", str(exc))


def _html_page(title: str, body: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: -apple-system, sans-serif; max-width: 600px;
                   margin: 60px auto; padding: 0 20px; color: #333; }}
            h1 {{ font-size: 24px; }}
            ul {{ line-height: 1.8; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <p>{body}</p>
    </body>
    </html>
    """, 200, {"Content-Type": "text/html"}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
