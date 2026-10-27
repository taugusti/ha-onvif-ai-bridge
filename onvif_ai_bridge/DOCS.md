# ONVIF AI Bridge

Many NVRs (for example the Ajax NVR) only react to the plain ONVIF motion event
from third-party cameras. Cameras such as Milesight send their AI detections
(human / vehicle, "Advanced Motion Detection") under a different ONVIF topic,
`tns1:RuleEngine/MotionDetector/Motion`, which those NVRs ignore.

This add-on is a small ONVIF proxy per camera:

- every ONVIF request from the NVR is forwarded unchanged to the real camera
  (login is passed through, the bridge stores no camera passwords);
- video (RTSP) is relayed byte-for-byte through the bridge (port `listen_port + 1000`, e.g. 9201), because some NVRs (Ajax) open the stream on the IP the camera was added with;
- the AI event is rewritten to `tns1:VideoSource/MotionAlarm` and
  `tns1:RuleEngine/CellMotionDetector/Motion`;
- with `suppress_basic_motion: true` (default) the camera's own pixel-motion
  events are removed, so only AI detections trigger the NVR.

## Configuration

```yaml
advertise_host: ""        # empty = Home Assistant host IP (auto-detected)
log_level: info
cameras:
  - name: Indkørsel AI
    host: 10.99.98.125    # the real camera
    port: 80              # camera ONVIF/HTTP port
    listen_port: 8201     # one of 8201-8208
    suppress_basic_motion: true
```

In the NVR, add the camera manually as ONVIF with the Home Assistant IP and the
`listen_port`, and the camera's normal ONVIF username/password.

Status per camera: `http://<HA-IP>:<listen_port>/__status`
