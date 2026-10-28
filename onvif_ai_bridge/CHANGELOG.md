# Changelog

## 0.3.0
- `source_topics` per camera (comma separated), e.g. Reolink `tns1:RuleEngine/MyRuleDetector/PeopleDetect, tns1:RuleEngine/MyRuleDetector/VehicleDetect`.
- Topic discovery: every new event topic a camera sends is logged once and listed under `topics_seen` in `/__status`.
- AI state is tracked per topic and rule, so person and vehicle detections can overlap.

## 0.2.1
- Retry a camera request once when the camera closes the connection without a reply (seen on Milesight during 1 s PullMessages polling from Ajax); prevents the NVR from dropping its event subscription.

## 0.2.0
- RTSP relay: stream URLs now point at the bridge (port listen_port+1000, e.g. 9201) and are relayed to the camera. Needed for NVRs such as Ajax that open the stream on the IP the camera was added with.

## 0.1.1
- Debug log shows the ONVIF operation of every request/response; optional `trace: true` per camera logs response bodies.

## 0.1.0
- First version: forwards ONVIF to the real camera, translates `tns1:RuleEngine/MotionDetector/Motion` (Milesight Advanced Motion Detection) into `tns1:VideoSource/MotionAlarm` and `tns1:RuleEngine/CellMotionDetector/Motion`, optionally suppresses the camera's own pixel motion. PullPoint and push subscriptions.
