#!/usr/bin/env python3
"""ONVIF AI bridge.

Sits between an NVR (e.g. Ajax NVR) and an ONVIF camera (e.g. Milesight).
Every ONVIF request is forwarded unchanged to the real camera; video (RTSP)
still goes directly from the camera to the NVR.

Only events are rewritten:
  * the camera's AI event (default: tns1:RuleEngine/MotionDetector/Motion,
    which Milesight uses for "Advanced Motion Detection" human/vehicle) is
    translated into the plain motion events NVRs understand:
      - tns1:VideoSource/MotionAlarm            (State)
      - tns1:RuleEngine/CellMotionDetector/Motion (IsMotion)
  * the camera's own plain (pixel) motion events can be suppressed, so only
    AI detections reach the NVR.

Both PullPoint (PullMessages) and push (Basic Notification / Subscribe)
event delivery are supported. Standard library only.
"""

import http.client
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("onvif_ai_bridge")

DEFAULT_SOURCE_TOPICS = ["tns1:RuleEngine/MotionDetector/Motion"]
BASIC_MOTION_TOPICS = {
    "tns1:VideoSource/MotionAlarm",
    "tns1:RuleEngine/CellMotionDetector/Motion",
}
TRUE_VALUES = {"true", "1"}

# Hop-by-hop / recomputed headers that must not be copied verbatim.
SKIP_REQ_HEADERS = {"host", "content-length", "connection", "accept-encoding",
                    "keep-alive", "proxy-connection", "transfer-encoding", "te"}
SKIP_RESP_HEADERS = {"content-length", "connection", "transfer-encoding",
                     "content-encoding", "keep-alive"}

NOTIF_RE = re.compile(
    r"<(?P<p>[\w.-]+:)?NotificationMessage\b[^>]*>.*?</(?P=p)?NotificationMessage>",
    re.S)
TOPIC_RE = re.compile(r"<(?P<p>[\w.-]+:)?Topic\b[^>]*>\s*(?P<t>[^<]+?)\s*</(?P=p)?Topic>", re.S)
TT_MESSAGE_RE = re.compile(r"<(?P<p>[\w.-]+:)?Message\b(?P<attrs>[^>]*\bUtcTime=[^>]*)>", re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
SIMPLE_ITEM_RE = re.compile(r'<(?:[\w.-]+:)?SimpleItem\b[^>]*?Name="([^"]+)"[^>]*?Value="([^"]*)"')
SIMPLE_ITEM_RE2 = re.compile(r'<(?:[\w.-]+:)?SimpleItem\b[^>]*?Value="([^"]*)"[^>]*?Name="([^"]+)"')
CONSUMER_RE = re.compile(
    r"(<(?P<p>[\w.-]+:)?ConsumerReference\b[^>]*>\s*<(?P<a>[\w.-]+:)?Address\b[^>]*>)"
    r"\s*(?P<url>[^<]+?)\s*(</(?P=a)?Address>)",
    re.S)


def simple_items(xml):
    items = {}
    for name, value in SIMPLE_ITEM_RE.findall(xml):
        items[name] = value
    for value, name in SIMPLE_ITEM_RE2.findall(xml):
        items.setdefault(name, value)
    return items


class Camera:
    """Configuration and live AI state for one bridged camera."""

    def __init__(self, cfg, advertise_host):
        self.name = cfg.get("name") or cfg["host"]
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 80))
        self.listen_port = int(cfg["listen_port"])
        self.source_topics = set(cfg.get("source_topics") or DEFAULT_SOURCE_TOPICS)
        self.suppress_basic = bool(cfg.get("suppress_basic_motion", True))
        self.advertise_host = advertise_host
        self.rules = {}          # rule name -> bool
        self.lock = threading.Lock()
        self.push_targets = {}   # token -> original consumer URL
        self.stats = {"requests": 0, "ai_events": 0, "translated": 0,
                      "suppressed": 0, "pushed": 0, "last_ai_change": None,
                      "motion": False}

    # ---- URL rewriting -------------------------------------------------
    @property
    def bridge_base(self):
        return f"http://{self.advertise_host}:{self.listen_port}"

    def rewrite_urls(self, text):
        base = self.bridge_base
        cam = re.escape(self.host)
        if self.port == 80:
            pattern = rf"http://{cam}(?::80)?(?=[/<\"'\s])"
        else:
            pattern = rf"http://{cam}:{self.port}(?=[/<\"'\s])"
        return re.sub(pattern, base, text)

    # ---- event rewriting ----------------------------------------------
    def aggregate(self):
        return any(self.rules.values())

    def translate_events(self, body):
        """Rewrite NotificationMessages inside a SOAP body."""
        if "NotificationMessage" not in body:
            return body

        def repl(m):
            block = m.group(0)
            wsnt = m.group("p") or ""
            tm = TOPIC_RE.search(block)
            if not tm:
                return block
            topic = tm.group("t").strip()
            if topic in self.source_topics:
                return self._translate_block(block, wsnt, topic)
            if self.suppress_basic and topic in BASIC_MOTION_TOPICS:
                self.stats["suppressed"] += 1
                LOG.debug("%s: suppressed camera basic motion event %s", self.name, topic)
                return ""
            return block

        return NOTIF_RE.sub(repl, body)

    def _translate_block(self, block, wsnt, topic):
        items = simple_items(block)
        rule = items.get("Rule", topic)
        value = None
        for key in ("IsMotion", "State", "IsInside", "IsCrossed"):
            if key in items:
                value = items[key]
                break
        mm = TT_MESSAGE_RE.search(block)
        tt = (mm.group("p") or "") if mm else "tt:"
        attrs = dict(ATTR_RE.findall(mm.group("attrs"))) if mm else {}
        utc = attrs.get("UtcTime") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        op = attrs.get("PropertyOperation", "Changed")
        if value is None:
            LOG.warning("%s: AI event %s without a state value, passed through", self.name, topic)
            return block
        state = value.strip().lower() in TRUE_VALUES
        with self.lock:
            before = self.aggregate()
            self.rules[rule] = state
            now = self.aggregate()
            self.stats["ai_events"] += 1
            self.stats["translated"] += 1
            self.stats["motion"] = now
            if now != before:
                self.stats["last_ai_change"] = f"{utc} {'ON' if now else 'OFF'}"
        if now != before or op != "Changed":
            LOG.info("%s: AI %s %s=%s -> motion %s (%s)", self.name, topic, rule,
                     state, "ON" if now else "off", op)
        v = "true" if now else "false"
        dialect = 'Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"'

        def msg(topic_out, source_items, data_items):
            src = "".join(f'<{tt}SimpleItem Name="{n}" Value="{val}"/>' for n, val in source_items)
            dat = "".join(f'<{tt}SimpleItem Name="{n}" Value="{val}"/>' for n, val in data_items)
            return (f"<{wsnt}NotificationMessage>"
                    f"<{wsnt}Topic {dialect}>{topic_out}</{wsnt}Topic>"
                    f"<{wsnt}Message><{tt}Message UtcTime=\"{utc}\" PropertyOperation=\"{op}\">"
                    f"<{tt}Source>{src}</{tt}Source>"
                    f"<{tt}Data>{dat}</{tt}Data>"
                    f"</{tt}Message></{wsnt}Message>"
                    f"</{wsnt}NotificationMessage>")

        vsc = items.get("VideoSourceConfigurationToken", "VideoSource")
        vac = items.get("VideoAnalyticsConfigurationToken", "VideoAnalyticsToken")
        return (msg("tns1:VideoSource/MotionAlarm", [("Source", vsc)], [("State", v)])
                + msg("tns1:RuleEngine/CellMotionDetector/Motion",
                      [("VideoSourceConfigurationToken", vsc),
                       ("VideoAnalyticsConfigurationToken", vac),
                       ("Rule", "MyCellMotionDetectorRule")],
                      [("IsMotion", v)]))

    # ---- push subscriptions --------------------------------------------
    def rewrite_subscribe_request(self, body):
        """Point a Basic-Notification Subscribe at the bridge instead of the NVR."""
        if "ConsumerReference" not in body or "Subscribe" not in body:
            return body

        def repl(m):
            url = m.group("url").strip()
            token = uuid.uuid4().hex
            self.push_targets[token] = url
            LOG.info("%s: push subscription, NVR consumer %s -> bridge token %s",
                     self.name, url, token)
            return f"{m.group(1)}{self.bridge_base}/__notify/{token}{m.group(5)}"

        return CONSUMER_RE.sub(repl, body, count=1)


def is_xml(body_bytes, content_type):
    ct = (content_type or "").lower()
    if "xml" in ct or "soap" in ct:
        return True
    head = body_bytes[:200].lstrip()
    return head.startswith(b"<?xml") or b"Envelope" in head


def forward(host, port, method, path, headers, body, timeout=130):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.reason, resp.getheaders(), data
    finally:
        conn.close()


def make_handler(cam):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "onvif-ai-bridge"

        def log_message(self, fmt, *args):  # route to logging
            LOG.debug("%s: %s %s", cam.name, self.address_string(), fmt % args)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _send(self, status, reason, headers, data):
            self.send_response(status, reason)
            for k, v in headers:
                if k.lower() not in SKIP_RESP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _proxy(self):
            cam.stats["requests"] += 1
            body = self._read_body()
            if self.path.startswith("/__notify/"):
                return self._push_in(body)
            if self.path == "/__status" and self.command == "GET":
                data = json.dumps({"camera": cam.name, "rules": cam.rules, **cam.stats},
                                  indent=2).encode()
                return self._send(200, "OK", [("Content-Type", "application/json")], data)

            text_req = None
            if body and is_xml(body, self.headers.get("Content-Type")):
                text_req = body.decode("utf-8", "replace")
                text_req = cam.rewrite_subscribe_request(text_req)
                body = text_req.encode("utf-8")

            headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_REQ_HEADERS}
            headers["Host"] = cam.host if cam.port == 80 else f"{cam.host}:{cam.port}"
            if body:
                headers["Content-Length"] = str(len(body))
            try:
                status, reason, rheaders, data = forward(cam.host, cam.port, self.command,
                                                         self.path, headers, body or None)
            except Exception as exc:  # camera unreachable
                LOG.warning("%s: camera request %s %s failed: %s", cam.name,
                            self.command, self.path, exc)
                return self._send(502, "Bad Gateway", [("Content-Type", "text/plain")],
                                  f"camera unreachable: {exc}".encode())

            ctype = dict((k.lower(), v) for k, v in rheaders).get("content-type")
            if data and is_xml(data, ctype):
                text = data.decode("utf-8", "replace")
                text = cam.rewrite_urls(text)
                text = cam.translate_events(text)
                data = text.encode("utf-8")
            self._send(status, reason, rheaders, data)

        def _push_in(self, body):
            """Camera pushes a Notify to the bridge; translate and relay to the NVR."""
            token = self.path.split("/")[2] if len(self.path.split("/")) > 2 else ""
            target = cam.push_targets.get(token)
            if not target:
                LOG.warning("%s: Notify for unknown token %s", cam.name, token)
                return self._send(404, "Not Found", [], b"")
            text = body.decode("utf-8", "replace")
            text = cam.translate_events(text)
            out = text.encode("utf-8")
            m = re.match(r"http://([^/:]+)(?::(\d+))?(/.*)?$", target)
            if not m:
                return self._send(502, "Bad Gateway", [], b"")
            host, port, path = m.group(1), int(m.group(2) or 80), m.group(3) or "/"
            headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_REQ_HEADERS}
            headers["Host"] = host if port == 80 else f"{host}:{port}"
            headers["Content-Length"] = str(len(out))
            try:
                status, reason, rheaders, data = forward(host, port, "POST", path, headers, out, 15)
                cam.stats["pushed"] += 1
            except Exception as exc:
                LOG.warning("%s: relay Notify to NVR %s failed: %s", cam.name, target, exc)
                return self._send(502, "Bad Gateway", [], b"")
            self._send(status, reason, rheaders, data)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _proxy

    return Handler


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def detect_host_ip():
    """Ask the HA Supervisor for the host's primary IPv4 address."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        conn = http.client.HTTPConnection("supervisor", 80, timeout=10)
        conn.request("GET", "/network/info", headers={"Authorization": f"Bearer {token}"})
        data = json.loads(conn.getresponse().read())
        ifaces = data.get("data", {}).get("interfaces", [])
        ifaces.sort(key=lambda i: not i.get("primary"))
        for iface in ifaces:
            for addr in (iface.get("ipv4") or {}).get("address") or []:
                return addr.split("/")[0]
    except Exception as exc:
        LOG.warning("Could not detect host IP from Supervisor: %s", exc)
    return None


def serve(config):
    advertise = config.get("advertise_host") or detect_host_ip()
    if not advertise:
        raise SystemExit("advertise_host not set and host IP could not be detected")
    servers = []
    for cfg in config.get("cameras", []):
        cam = Camera(cfg, advertise)
        srv = ThreadingHTTPServer(("0.0.0.0", cam.listen_port), make_handler(cam))
        srv.daemon_threads = True
        t = threading.Thread(target=srv.serve_forever, name=cam.name, daemon=True)
        t.start()
        servers.append((cam, srv))
        LOG.info("Bridge '%s': http://%s:%d -> camera %s:%d (AI topics: %s, suppress basic motion: %s)",
                 cam.name, advertise, cam.listen_port, cam.host, cam.port,
                 ", ".join(sorted(cam.source_topics)), cam.suppress_basic)
    return servers


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BRIDGE_CONFIG", "/data/options.json")
    config = load_config(path)
    logging.basicConfig(
        level=getattr(logging, str(config.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")
    servers = serve(config)
    if not servers:
        LOG.error("No cameras configured")
        sys.exit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
