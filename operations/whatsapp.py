# sends one WhatsApp message. Two ways, chosen in the settings file:
#   file      writes each message to output/messages/ instead of sending it, for trying things out
#   wasender  WasenderAPI (wasenderapi.com). A paid service that sends from a WhatsApp number
#             linked to it by QR code, the way WhatsApp Web is linked. It is not Meta's official
#             API, so WhatsApp can restrict or ban that number. Use a separate SIM, not the owner's.
#
# The WasenderAPI key comes from the WASENDER_API_KEY environment variable, never from a file.

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# +91 then the number, nothing else
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")

WASENDER_URL = "https://www.wasenderapi.com/api/send-message"
PROVIDERS = ("file", "wasender")


@dataclass
class Sent:
    ok: bool
    message_id: str = ""
    error: str = ""
    # worth trying again later: the network, the service being busy, the phone being unlinked
    retry: bool = False
    # seconds to wait, when the service says
    retry_after: float = 0


class SendError(Exception):
    pass


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "message"


class FileSender:
    name = "file"

    def __init__(self, folder="output/messages"):
        self.folder = Path(folder)

    def send(self, phone, text, label="message"):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{safe_name(label)}.txt"
        path.write_text(f"to: {phone}\n\n{text}\n", encoding="utf-8")
        return Sent(ok=True, message_id=str(path))


def read_reply(status, raw, headers=None):
    # turns WasenderAPI's answer into a Sent
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}

    if 200 <= status < 300 and body.get("success") is True:
        data = body.get("data") or {}
        return Sent(ok=True, message_id=str(data.get("msgId", "")))

    message = str(body.get("message") or "no message given")
    if body.get("errors"):
        message += " " + json.dumps(body["errors"], ensure_ascii=False)

    wait = body.get("retry_after")
    if wait is None and headers is not None:
        wait = headers.get("Retry-After")
    try:
        wait = max(float(wait), 0) if wait is not None else 0
    except (TypeError, ValueError):
        wait = 0

    retry = (
        wait > 0
        or status in (408, 425, 429)
        or status >= 500
        # the linked phone is offline or was unlinked. It can come back.
        or "not connected" in message.lower()
        # an answer that is neither a success nor an error
        or 200 <= status < 300
    )
    return Sent(ok=False, error=f"HTTP {status}: {message}"[:500], retry=retry, retry_after=wait)


class WasenderSender:
    name = "wasender"

    def __init__(self, api_key=None, url=WASENDER_URL, timeout=20, opener=urllib.request.urlopen):
        self.api_key = api_key or os.environ.get("WASENDER_API_KEY")
        if not self.api_key:
            raise SendError("no WasenderAPI key. Set WASENDER_API_KEY first.")
        self.url = url
        self.timeout = timeout
        self.opener = opener

    def send(self, phone, text, label=""):
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"to": phone, "text": text}, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "machine-monitoring",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as r:
                return read_reply(r.status, r.read(), r.headers)
        except urllib.error.HTTPError as e:
            return read_reply(e.code, e.read(), e.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # no internet at the shop is normal for a while. Try again later.
            reason = getattr(e, "reason", e)
            return Sent(ok=False, error=f"network: {reason}"[:500], retry=True)


def make_sender(settings):
    provider = settings["whatsapp"]["provider"]
    if provider == "wasender":
        return WasenderSender()
    return FileSender()
