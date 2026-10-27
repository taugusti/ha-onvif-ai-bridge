# Changelog

## 0.2.0
- RTSP relay: stream URLs now point at the bridge (port listen_port+1000, e.g. 9201) and are relayed to the camera. Needed for NVRs such as Ajax that open the stream on the IP the camera was added with.

## 0.1.1
- Debug log shows the ONVIF operation of every request/response; optional `trace: true` per camera logs response bodies.

## 0.1.0
- First version: forwards ONVIF to the real camera, translates `tns1:RuleEngine/MotionDetector/Motion` (Milesight Advanced Motion Detection) into `tns1:VideoSource/MotionAlarm` and `tns1:RuleEngine/CellMotionDetector/Motion`, optionally suppresses the camera's own pixel motion. PullPoint and push subscriptions.
