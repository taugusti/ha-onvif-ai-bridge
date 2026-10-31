import http.client
import socket
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "onvif_ai_bridge", "app"))
import bridge  # noqa: E402

NS = ('xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
      'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
      'xmlns:tns1="http://www.onvif.org/ver10/topics" xmlns:wsa5="http://www.w3.org/2005/08/addressing"')


def notif(topic, rule, key, value, op="Changed"):
    # Copied in shape from the real Milesight MS-C5375-FPD PullMessages response
    src_rule = f'<tt:SimpleItem Name="Rule" Value="{rule}" />' if rule else ""
    if topic == "tns1:VideoSource/MotionAlarm":
        source = '<tt:SimpleItem Name="Source" Value="VideoSource" />'
    else:
        source = ('<tt:SimpleItem Name="VideoSourceConfigurationToken" Value="VideoSource" />'
                  '<tt:SimpleItem Name="VideoAnalyticsConfigurationToken" Value="VideoAnalyticsToken" />'
                  + src_rule)
    return (f'<wsnt:NotificationMessage><wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">{topic}</wsnt:Topic>'
            f'<wsnt:Message><tt:Message UtcTime="2026-10-27T19:09:20Z" PropertyOperation="{op}">              '
            f'<tt:Source>              {source}              </tt:Source>'
            f'              <tt:Key><tt:SimpleItem Name="ObjectId" Value="0" /></tt:Key>'
            f'              <tt:Data><tt:SimpleItem Name="{key}" Value="{value}" /></tt:Data>'
            f'              </tt:Message></wsnt:Message></wsnt:NotificationMessage>')


def envelope(body):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<SOAP-ENV:Envelope {NS}><SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>\r\n'


class FakeCamera:
    def __init__(self):
        self.requests = []
        self.pull_queue = []
        self.pushed_to = None
        cam = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                cam.requests.append((self.path, dict(self.headers), body))
                if "GetCapabilities" in body:
                    out = envelope(f'<tds:GetCapabilitiesResponse><tt:XAddr>http://{cam.host}:{cam.port}/onvif/Events</tt:XAddr>'
                                   f'<tt:XAddr>http://{cam.host}:{cam.port}/onvif/Media</tt:XAddr></tds:GetCapabilitiesResponse>')
                elif "GetStreamUri" in body:
                    out = envelope(f'<trt:MediaUri><tt:Uri>rtsp://{cam.host}:554/main</tt:Uri></trt:MediaUri>')
                elif "CreatePullPointSubscription" in body:
                    out = envelope(f'<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>'
                                   f'<wsa5:Address>http://{cam.host}:{cam.port}/onvif/Events/SubManager_0</wsa5:Address>'
                                   f'</tev:SubscriptionReference></tev:CreatePullPointSubscriptionResponse>')
                elif "PullMessages" in body:
                    msgs = "".join(cam.pull_queue)
                    cam.pull_queue = []
                    out = envelope(f'<tev:PullMessagesResponse><tev:CurrentTime>x</tev:CurrentTime>{msgs}</tev:PullMessagesResponse>')
                elif "<wsnt:Subscribe" in body:
                    import re
                    cam.pushed_to = re.search(r"<wsa5:Address>([^<]+)</wsa5:Address>", body).group(1)
                    out = envelope('<wsnt:SubscribeResponse/>')
                else:
                    out = envelope('<x/>')
                data = out.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = "127.0.0.1"
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


class FakeNVRConsumer:
    def __init__(self):
        self.received = []
        nvr = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                nvr.received.append(self.rfile.read(n).decode())
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def post(port, path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=body.encode(), headers={"Content-Type": "application/soap+xml"})
    r = c.getresponse()
    return r.status, r.read().decode()


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cam = FakeCamera()
        # find a free port for the bridge
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.bport = s.getsockname()[1]; s.close()
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.rport = s.getsockname()[1]; s.close()
        # fake "camera RTSP" server: echoes what it receives, prefixed
        cls.rtsp_srv = socket.socket(); cls.rtsp_srv.bind(("127.0.0.1", 0)); cls.rtsp_srv.listen(4)
        cls.cam_rtsp_port = cls.rtsp_srv.getsockname()[1]

        def rtsp_echo():
            while True:
                c, _ = cls.rtsp_srv.accept()
                data = c.recv(4096)
                c.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nX-Echo: " + data.split(b"\r\n")[0] + b"\r\n\r\n")
                c.close()
        threading.Thread(target=rtsp_echo, daemon=True).start()
        cfg = {"advertise_host": "10.99.98.216",
               "cameras": [{"name": "Indkørsel AI", "host": "127.0.0.1", "port": cls.cam.port,
                            "listen_port": cls.bport, "rtsp_port": cls.cam_rtsp_port,
                            "rtsp_listen_port": cls.rport}]}
        cls.servers = bridge.serve(cfg)
        cls.bcam = cls.servers[0][0]
        time.sleep(0.2)

    def test_urls_and_rtsp_rewritten(self):
        st, body = post(self.bport, "/onvif/device_service", envelope("<tds:GetCapabilities/>"))
        self.assertEqual(st, 200)
        # fake camera is not on port 80, so only host:port form is rewritten
        self.assertIn(f"http://10.99.98.216:{self.bport}/onvif/Events", body)
        st, body = post(self.bport, "/onvif/Media", envelope("<trt:GetStreamUri/>"))
        # fake camera advertises :554 but its RTSP port in this test is cam_rtsp_port -> untouched
        self.assertIn("rtsp://127.0.0.1:554/main", body)
        cam = bridge.Camera({"host": "127.0.0.1", "listen_port": 8201}, "10.99.98.216")
        self.assertEqual(cam.rewrite_urls("<u>rtsp://127.0.0.1:554/main</u>"),
                         "<u>rtsp://10.99.98.216:9201/main</u>")

    def test_rtsp_relay(self):
        c = socket.create_connection(("127.0.0.1", self.rport), timeout=5)
        c.sendall(b"OPTIONS rtsp://10.99.98.216:9201/main RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
        c.close()
        self.assertIn(b"RTSP/1.0 200 OK", data)
        self.assertIn(b"X-Echo: OPTIONS rtsp://10.99.98.216:9201/main RTSP/1.0", data)

    def test_pullpoint_translation_and_suppression(self):
        st, body = post(self.bport, "/onvif/Events", envelope("<tev:CreatePullPointSubscription/>"))
        self.assertIn(f"http://10.99.98.216:{self.bport}/onvif/Events/SubManager_0", body)

        # camera reports plain pixel motion only -> must be suppressed
        self.cam.pull_queue = [notif("tns1:VideoSource/MotionAlarm", None, "State", "true"),
                               notif("tns1:RuleEngine/CellMotionDetector/Motion", "MyCellMotionDetectorRule", "IsMotion", "true")]
        st, body = post(self.bport, "/onvif/Events/SubManager_0", envelope("<tev:PullMessages/>"))
        self.assertNotIn("NotificationMessage", body)

        # AI rule 1 true -> MotionAlarm true + CellMotion true
        self.cam.pull_queue = [notif("tns1:RuleEngine/MotionDetector/Motion", "MyMotionDetectorRule1", "IsMotion", "true")]
        st, body = post(self.bport, "/onvif/Events/SubManager_0", envelope("<tev:PullMessages/>"))
        self.assertIn("tns1:VideoSource/MotionAlarm", body)
        self.assertIn('Name="State" Value="true"', body)
        self.assertIn('Name="IsMotion" Value="true"', body)
        self.assertNotIn("tns1:RuleEngine/MotionDetector/Motion", body)

        # rule 2 true, rule 1 false -> still motion (aggregate)
        self.cam.pull_queue = [notif("tns1:RuleEngine/MotionDetector/Motion", "MyMotionDetectorRule2", "IsMotion", "true"),
                               notif("tns1:RuleEngine/MotionDetector/Motion", "MyMotionDetectorRule1", "IsMotion", "false")]
        st, body = post(self.bport, "/onvif/Events/SubManager_0", envelope("<tev:PullMessages/>"))
        self.assertNotIn('Value="false"', body.split("NotificationMessage")[-2])

        # rule 2 false -> motion off
        self.cam.pull_queue = [notif("tns1:RuleEngine/MotionDetector/Motion", "MyMotionDetectorRule2", "IsMotion", "false")]
        st, body = post(self.bport, "/onvif/Events/SubManager_0", envelope("<tev:PullMessages/>"))
        self.assertIn('Name="State" Value="false"', body)

        # unrelated events pass through
        self.cam.pull_queue = [notif("tns1:RuleEngine/TamperDetector/Tamper", "MyTamperDetectorRule", "IsTamper", "false")]
        st, body = post(self.bport, "/onvif/Events/SubManager_0", envelope("<tev:PullMessages/>"))
        self.assertIn("TamperDetector", body)

    def test_push_subscription_relay(self):
        nvr = FakeNVRConsumer()
        sub = envelope(f'<wsnt:Subscribe><wsnt:ConsumerReference><wsa5:Address>http://127.0.0.1:{nvr.port}/ajax/notify'
                       f'</wsa5:Address></wsnt:ConsumerReference></wsnt:Subscribe>')
        st, body = post(self.bport, "/onvif/Events", sub)
        self.assertEqual(st, 200)
        self.assertTrue(self.cam.pushed_to.startswith(f"http://10.99.98.216:{self.bport}/__notify/"))
        path = self.cam.pushed_to.split(str(self.bport), 1)[1]
        notify = envelope("<wsnt:Notify>" + notif("tns1:RuleEngine/MotionDetector/Motion", "MyMotionDetectorRule1", "IsMotion", "true") + "</wsnt:Notify>")
        st, _ = post(self.bport, path, notify)
        self.assertEqual(st, 200)
        self.assertEqual(len(nvr.received), 1)
        self.assertIn("tns1:VideoSource/MotionAlarm", nvr.received[0])
        self.assertIn('Name="State" Value="true"', nvr.received[0])

    def test_auth_headers_forwarded(self):
        c = http.client.HTTPConnection("127.0.0.1", self.bport, timeout=10)
        c.request("POST", "/onvif/device_service", body=envelope("<x/>").encode(),
                  headers={"Authorization": 'Digest username="admin", uri="/onvif/device_service"',
                           "Content-Type": "application/soap+xml"})
        c.getresponse().read()
        self.assertIn("Authorization", self.cam.requests[-1][1])

    def test_multiple_source_topics_reolink_style(self):
        cam = bridge.Camera({"host": "10.99.98.239", "listen_port": 8204, "port": 8000,
                             "source_topics": "tns1:RuleEngine/MyRuleDetector/PeopleDetect, tns1:RuleEngine/MyRuleDetector/VehicleDetect"},
                            "10.99.98.216")
        people_on = notif("tns1:RuleEngine/MyRuleDetector/PeopleDetect", None, "State", "true")
        car_on = notif("tns1:RuleEngine/MyRuleDetector/VehicleDetect", None, "State", "true")
        people_off = notif("tns1:RuleEngine/MyRuleDetector/PeopleDetect", None, "State", "false")
        car_off = notif("tns1:RuleEngine/MyRuleDetector/VehicleDetect", None, "State", "false")
        pixel = notif("tns1:VideoSource/MotionAlarm", None, "State", "true")
        out = cam.translate_events(envelope(pixel + people_on))
        self.assertEqual(out.count("tns1:VideoSource/MotionAlarm"), 1)   # pixel dropped, AI translated
        self.assertIn('Name="State" Value="true"', out)
        cam.translate_events(envelope(car_on + people_off))
        self.assertTrue(cam.aggregate())                                   # vehicle still active
        out = cam.translate_events(envelope(car_off))
        self.assertIn('Name="State" Value="false"', out)
        self.assertIn("tns1:RuleEngine/MyRuleDetector/PeopleDetect", cam.topics_seen)

    def test_ha_entity_injection(self):
        cam = bridge.Camera({"host": "10.99.98.239", "listen_port": 8205, "port": 8000,
                             "ha_entities": "binary_sensor.doorbell_person, binary_sensor.doorbell_vehicle",
                             "suppress_basic_motion": True}, "10.99.98.216")
        pull = envelope('<tev:PullMessagesResponse><tev:CurrentTime>x</tev:CurrentTime>'
                        + notif("tns1:VideoSource/MotionAlarm", None, "State", "true")
                        + '</tev:PullMessagesResponse>')
        import xml.etree.ElementTree as ET
        # first pull on a subscription -> Initialized false, camera pixel motion removed
        out = cam.inject_pull_response("/onvif/Events/Sub1", cam.translate_events(pull))
        ET.fromstring(out.encode())
        self.assertIn('PropertyOperation="Initialized"', out)
        self.assertIn('Name="State" Value="false"', out)
        # no change -> nothing injected
        out = cam.inject_pull_response("/onvif/Events/Sub1", cam.translate_events(pull))
        self.assertNotIn("NotificationMessage", out)
        # HA person on -> Changed true
        cam.set_ha_state("binary_sensor.doorbell_person", True)
        out = cam.inject_pull_response("/onvif/Events/Sub1", cam.translate_events(pull))
        ET.fromstring(out.encode())
        self.assertIn('Name="State" Value="true"', out)
        self.assertIn('PropertyOperation="Changed"', out)
        cam.set_ha_state("binary_sensor.doorbell_person", False)
        out = cam.inject_pull_response("/onvif/Events/Sub1", cam.translate_events(pull))
        self.assertIn('Name="State" Value="false"', out)

    def test_pullpoint_lifetime_rewrite(self):
        import xml.etree.ElementTree as ET
        cam = bridge.Camera({"host": "10.99.98.125", "listen_port": 8201}, "10.99.98.216")
        hdr = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
               'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
               'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
               '<s:Header><wsse:Security><wsse:UsernameToken><wsse:Username>admin</wsse:Username>'
               '</wsse:UsernameToken></wsse:Security></s:Header><s:Body>')
        end = '</s:Body></s:Envelope>'
        # existing value replaced
        a = cam.rewrite_pullpoint_request(hdr + '<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT10S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>' + end)
        self.assertIn(">PT300S<", a); self.assertNotIn("PT10S", a); ET.fromstring(a)
        # missing element inserted (self-closing form)
        b = cam.rewrite_pullpoint_request(hdr + '<tev:CreatePullPointSubscription/>' + end)
        self.assertIn("<tev:InitialTerminationTime>PT300S</tev:InitialTerminationTime>", b); ET.fromstring(b)
        # missing element inserted after Filter
        c = cam.rewrite_pullpoint_request(hdr + '<tev:CreatePullPointSubscription><tev:Filter><x/></tev:Filter></tev:CreatePullPointSubscription>' + end)
        self.assertIn("</tev:Filter><tev:InitialTerminationTime>PT300S", c); ET.fromstring(c)
        # Renew built from a PullMessages request carries the Security header and parses
        r = cam.build_renew(hdr + '<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout></tev:PullMessages>' + end)
        root = ET.fromstring(r.encode())
        self.assertIn("wsse:UsernameToken", r)
        self.assertIn("PT300S", r)
        self.assertTrue(root.tag.endswith("Envelope"))

    def test_port80_url_forms(self):
        cam = bridge.Camera({"host": "10.99.98.125", "listen_port": 8125}, "10.99.98.216")
        s = ("<a>http://10.99.98.125:80/onvif/Events</a><b>http://10.99.98.125/onvif/Media</b>"
             "<c>rtsp://10.99.98.125:554/x</c><d>http://10.99.98.1250/x</d>")
        out = cam.rewrite_urls(s)
        self.assertIn("http://10.99.98.216:8125/onvif/Events", out)
        self.assertIn("http://10.99.98.216:8125/onvif/Media", out)
        self.assertIn("rtsp://10.99.98.216:9125/x", out)
        self.assertIn("http://10.99.98.1250/x", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
