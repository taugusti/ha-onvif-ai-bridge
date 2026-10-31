# Changelog

## 0.5.1
- Renew carries WS-Addressing Action/To/MessageID; refused renewals log the fault reason.

## 0.5.0
- Fix missed events: Milesight grants PullPoint subscriptions only 10 s and Ajax never renews them, leaving the NVR blind ~10 s of every ~22 s. The bridge now requests a longer `InitialTerminationTime` (`subscription_seconds`, default 300) and renews active subscriptions in the background every `renew_interval` seconds (default 60), reusing the NVR's own credentials.

## 0.4.0
- `ha_entities` per camera (comma separated binary_sensors): for cameras that do not send AI over ONVIF (e.g. Reolink doorbell), Home Assistant sensors such as `binary_sensor.doorbell_person` drive the motion event that the NVR sees. Injected into PullPoint responses.

## 0.3.1
- `trace_chars` option to log longer response bodies (e.g. GetEventProperties).
- Only real motion changes are logged at info; re-subscription states go to debug.

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
