# sends one WhatsApp message. Three ways, chosen in the settings file:
#   file      writes each message to output/messages/ instead of sending it, for trying things out
#   meta      Meta's official WhatsApp Cloud API. Starts the chat with an approved template.
#             The token comes from the WHATSAPP_TOKEN environment variable.
#   wasender  WasenderAPI (wasenderapi.com). A paid service that sends from a WhatsApp number
#             linked to it by QR code, the way WhatsApp Web is linked. It is not Meta's official
#             API, so WhatsApp can restrict or ban that number. Use a separate SIM, not the owner's.
#             The key comes from the WASENDER_API_KEY environment variable.
#
# Keys and tokens never go in a file in the repo.

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
META_URL = "https://graph.facebook.com/{version}/{phone_number_id}/messages"
PROVIDERS = ("file", "meta", "wasender")

# the template language codes Meta uses for each message language
META_LANGUAGES = {"en": "en", "mr": "mr", "hi": "hi"}

# Meta error codes worth trying again later: busy, rate limits, temporary faults.
# Everything else (a bad token, a wrong template, a number not allowed) needs a person.
META_RETRY = {1, 2, 4, 80007, 130429, 131000, 131016, 131056, 133004}
# "not delivered to maintain healthy ecosystem engagement": wait at least a day
META_WAIT_A_DAY = {131049}

# what to do about the errors a first setup usually hits
META_HINTS = {
    0: "the token is not valid. Make a new one",
    190: "the token has expired. The temporary token from API Setup lasts only a short while",
    10: "the token is missing a permission (whatsapp_business_messaging)",
    100: "a setting is wrong, often the phone number ID",
    131030: "on the test number, add this phone as a recipient in API Setup first",
    131026: "this phone cannot receive WhatsApp messages",
    132000: "the template has a different number of values. Submit it again from --template",
    132001: "the template is not approved yet, or not in this language",
    132005: "the message is too long for the template",
    132015: "the template was paused by WhatsApp for low quality",
    133010: "the business phone number is not registered",
}


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


def post_json(url, body, token, opener, timeout):
    # returns (status, raw body, headers), or a Sent when the network itself failed
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "machine-monitoring",
        },
    )
    try:
        with opener(request, timeout=timeout) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # no internet at the shop is normal for a while. Try again later.
        reason = getattr(e, "reason", e)
        return Sent(ok=False, error=f"network: {reason}"[:500], retry=True)


def json_body(raw):
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


class FileSender:
    name = "file"

    def __init__(self, folder="output/messages"):
        self.folder = Path(folder)

    def send(self, phone, text, label="message", **_):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{safe_name(label)}.txt"
        path.write_text(f"to: {phone}\n\n{text}\n", encoding="utf-8")
        return Sent(ok=True, message_id=str(path))

    def test(self, phone, text):
        return self.send(phone, text, "test")


# ---- Meta's WhatsApp Cloud API

def read_meta_reply(status, raw):
    body = json_body(raw)
    messages = body.get("messages") or []
    if 200 <= status < 300 and messages and messages[0].get("id"):
        # accepted. Meta reports delivery later, by webhook.
        return Sent(ok=True, message_id=str(messages[0]["id"]))

    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = error.get("code")
    details = (error.get("error_data") or {}).get("details") or ""
    text = f"Meta {code if code is not None else f'HTTP {status}'}: {error.get('message') or 'no message given'}"
    if details:
        text += f" ({details})"
    if code in META_HINTS:
        text += f". {META_HINTS[code]}"

    if code in META_WAIT_A_DAY:
        return Sent(ok=False, error=text[:500], retry=True, retry_after=24 * 3600)
    retry = code in META_RETRY or (code is None and (status >= 500 or status in (408, 429)))
    return Sent(ok=False, error=text[:500], retry=retry)


class MetaSender:
    name = "meta"

    def __init__(self, phone_number_id, template, version="v27.0", token=None, timeout=20,
                 opener=urllib.request.urlopen):
        self.token = token or os.environ.get("WHATSAPP_TOKEN")
        if not self.token:
            raise SendError("no WhatsApp token. Set WHATSAPP_TOKEN first.")
        if not phone_number_id:
            raise SendError("no phone number ID. Put meta_phone_number_id in the settings file.")
        self.url = META_URL.format(version=version, phone_number_id=phone_number_id)
        self.template = template
        self.timeout = timeout
        self.opener = opener

    def send_template(self, phone, name, language, values):
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "template",
            "template": {"name": name, "language": {"code": language}},
        }
        if values:
            body["template"]["components"] = [
                {"type": "body", "parameters": [{"type": "text", "text": str(v)} for v in values]}
            ]
        reply = post_json(self.url, body, self.token, self.opener, self.timeout)
        return reply if isinstance(reply, Sent) else read_meta_reply(reply[0], reply[1])

    def send(self, phone, text, label="", lang="en", params=None):
        # a chat can only be started with an approved template, so the free text is not used
        if not params:
            return Sent(ok=False, error="no template values for this message. Queue it again.")
        return self.send_template(phone, self.template, META_LANGUAGES[lang], params)

    def test(self, phone, text):
        # every new WhatsApp Business account has the hello_world template, already approved
        return self.send_template(phone, "hello_world", "en_US", [])


# ---- WasenderAPI

def read_reply(status, raw, headers=None):
    # turns WasenderAPI's answer into a Sent
    body = json_body(raw)

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

    def send(self, phone, text, label="", **_):
        reply = post_json(self.url, {"to": phone, "text": text}, self.api_key, self.opener, self.timeout)
        return reply if isinstance(reply, Sent) else read_reply(*reply)

    def test(self, phone, text):
        return self.send(phone, text)


def make_sender(settings):
    w = settings["whatsapp"]
    if w["provider"] == "meta":
        return MetaSender(w["meta_phone_number_id"], w["meta_template"], w["meta_api_version"])
    if w["provider"] == "wasender":
        return WasenderSender()
    return FileSender()
