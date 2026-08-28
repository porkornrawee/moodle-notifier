#!/bin/bash
# ตัวห่อสำหรับ launchd/cron — เขียน log ไว้ข้าง ๆ สคริปต์
cd "$(dirname "$0")" || exit 1
exec /usr/bin/env python3 ./moodle_watch.py "$@" >> ./moodle-watch.log 2>&1
