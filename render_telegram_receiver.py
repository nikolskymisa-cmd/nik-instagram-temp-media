import base64
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def load_config() -> dict:
    load_dotenv(Path(__file__).with_name(".env"), override=True)
    required = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_CHAT_ID",
        "GITHUB_TOKEN",
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "GITHUB_BRANCH",
        "RENDER_WEBHOOK_SECRET",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))
    return {
        "bot_token": os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        "allowed_chat_id": int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"].strip()),
        "allowed_thread_id": int(os.getenv("TELEGRAM_ALLOWED_THREAD_ID", "0") or "0"),
        "github_token": os.environ["GITHUB_TOKEN"].strip(),
        "github_owner": os.environ["GITHUB_OWNER"].strip(),
        "github_repo": os.environ["GITHUB_REPO"].strip(),
        "github_branch": os.environ["GITHUB_BRANCH"].strip(),
        "github_prefix": os.getenv("GITHUB_INBOX_PREFIX", "telegram-inbox").strip().strip("/"),
        "webhook_secret": os.environ["RENDER_WEBHOOK_SECRET"].strip(),
        "public_url": os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/") or os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        "max_photos": int(os.getenv("MAX_PHOTOS_PER_POST", "10")),
    }


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def github_headers(config: dict) -> dict:
    return {
        "Authorization": f"Bearer {config['github_token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_api_base(config: dict) -> str:
    return f"https://api.github.com/repos/{config['github_owner']}/{config['github_repo']}/contents"


def github_get(config: dict, key: str) -> dict | None:
    response = httpx.get(
        f"{github_api_base(config)}/{quote(key, safe='/')}",
        headers=github_headers(config),
        params={"ref": config["github_branch"]},
        timeout=httpx.Timeout(60, connect=20),
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def github_put(config: dict, key: str, content: bytes, message: str) -> None:
    existing = github_get(config, key)
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": config["github_branch"],
    }
    if existing and existing.get("sha"):
        payload["sha"] = existing["sha"]
    response = httpx.put(
        f"{github_api_base(config)}/{quote(key, safe='/')}",
        headers=github_headers(config),
        json=payload,
        timeout=httpx.Timeout(120, connect=20),
    )
    response.raise_for_status()


def telegram_api(config: dict, method: str) -> str:
    return f"https://api.telegram.org/bot{config['bot_token']}/{method}"


def telegram_file_bytes(config: dict, file_id: str) -> bytes:
    response = httpx.get(telegram_api(config, "getFile"), params={"file_id": file_id}, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getFile failed: {data}")
    file_path = data["result"]["file_path"]
    file_response = httpx.get(f"https://api.telegram.org/file/bot{config['bot_token']}/{file_path}", timeout=120)
    file_response.raise_for_status()
    return file_response.content


def set_webhook(config: dict) -> None:
    if not config["public_url"]:
        log("No PUBLIC_BASE_URL or RENDER_EXTERNAL_URL yet, webhook was not set.")
        return
    webhook_url = f"{config['public_url']}/telegram/{config['webhook_secret']}"
    response = httpx.post(
        telegram_api(config, "setWebhook"),
        json={
            "url": webhook_url,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    log(f"Telegram webhook set: {webhook_url}")


def message_from_update(update: dict) -> dict | None:
    return update.get("message") or update.get("channel_post")


def allowed(config: dict, message: dict) -> bool:
    chat = message.get("chat") or {}
    if int(chat.get("id", 0)) != config["allowed_chat_id"]:
        return False
    if config["allowed_thread_id"]:
        if int(message.get("message_thread_id", 0) or 0) != config["allowed_thread_id"]:
            return False
    return True


def image_info(message: dict) -> tuple[str, str, str] | None:
    if message.get("photo"):
        photo = message["photo"][-1]
        return photo["file_id"], photo.get("file_unique_id", photo["file_id"]), ".jpg"
    document = message.get("document") or {}
    mime_type = document.get("mime_type", "")
    if mime_type.startswith("image/"):
        return document["file_id"], document.get("file_unique_id", document["file_id"]), mimetypes.guess_extension(mime_type) or ".jpg"
    return None


def post_id(message: dict) -> str:
    stamp = datetime.fromtimestamp(message.get("date", 0), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
    if message.get("media_group_id"):
        return f"{stamp}_album_{message['media_group_id']}"
    return f"{stamp}_message_{message.get('message_id')}"


def next_photo_name(config: dict, pid: str, extension: str) -> str:
    folder_key = f"{config['github_prefix']}/{pid}"
    listing = github_get(config, folder_key)
    numbers = []
    if isinstance(listing, list):
        for item in listing:
            name = item.get("name", "")
            if name.startswith("photo_") and Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                digits = name.removeprefix("photo_").split(".", 1)[0]
                if digits.isdigit():
                    numbers.append(int(digits))
    number = max(numbers, default=0) + 1
    return f"photo_{number:02d}{extension.lower()}"


def read_remote_metadata(config: dict, pid: str) -> dict:
    key = f"{config['github_prefix']}/{pid}/metadata.json"
    existing = github_get(config, key)
    if not existing or "content" not in existing:
        return {}
    raw = base64.b64decode(existing["content"]).decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def save_remote_metadata(config: dict, pid: str, metadata: dict) -> None:
    key = f"{config['github_prefix']}/{pid}/metadata.json"
    github_put(
        config,
        key,
        json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
        f"Update Telegram inbox metadata {pid}",
    )


app = Flask(__name__)
CONFIG = load_config()


@app.get("/")
def index():
    return jsonify({"ok": True, "service": "nik-posts-render-receiver"})


@app.post("/telegram/<secret>")
def telegram_webhook(secret: str):
    if secret != CONFIG["webhook_secret"]:
        return Response("forbidden", status=403)

    update = request.get_json(force=True, silent=True) or {}
    message = message_from_update(update)
    if not message or not allowed(CONFIG, message):
        return jsonify({"ok": True, "ignored": True})

    info = image_info(message)
    if not info:
        return jsonify({"ok": True, "ignored": True})

    pid = post_id(message)
    file_id, unique_id, extension = info
    metadata = read_remote_metadata(CONFIG, pid)
    seen = set(metadata.get("seen_unique_ids", []))
    if unique_id in seen:
        return jsonify({"ok": True, "duplicate": True})

    photo_count = len(metadata.get("images", []))
    if photo_count >= CONFIG["max_photos"]:
        return jsonify({"ok": True, "skipped": "max_photos"})

    image_name = next_photo_name(CONFIG, pid, extension)
    image_bytes = telegram_file_bytes(CONFIG, file_id)
    image_key = f"{CONFIG['github_prefix']}/{pid}/{image_name}"
    github_put(CONFIG, image_key, image_bytes, f"Save Telegram photo {pid} {image_name}")

    seen.add(unique_id)
    images = metadata.get("images", [])
    if image_name not in images:
        images.append(image_name)
    message_ids = set(str(value) for value in metadata.get("message_ids", []))
    message_ids.add(str(message.get("message_id")))
    metadata.update(
        {
            "id": pid,
            "source": "render_telegram_receiver",
            "status": "ready",
            "chat_id": message.get("chat", {}).get("id"),
            "thread_id": message.get("message_thread_id"),
            "media_group_id": message.get("media_group_id"),
            "message_ids": sorted(message_ids, key=int),
            "seen_unique_ids": sorted(seen),
            "images": images,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_remote_metadata(CONFIG, pid, metadata)
    log(f"Saved Telegram photo to GitHub: {pid}/{image_name}")
    return jsonify({"ok": True, "post_id": pid, "image": image_name})


try:
    set_webhook(CONFIG)
except Exception as exc:
    log(f"Could not set Telegram webhook: {exc}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
