#!/usr/bin/env python3
"""
moodle_watch.py — เฝ้าดูคอร์สบน Moodle (courses.cs.tu.ac.th) แล้วแจ้งเตือนเมื่อมีของใหม่

โหมดการทำงาน (เลือกอัตโนมัติ):
  1) Web Service API  — ใช้ token จาก login/token.php  (แม่นยำที่สุด)
  2) HTML scraping    — ล็อกอินด้วย session แล้ว diff หน้าคอร์ส (ใช้เมื่อ WS ปิด)

ตรวจจับ: กิจกรรม/ไฟล์ใหม่, กิจกรรมถูกแก้ชื่อ/แก้กำหนดส่ง, ประกาศใหม่ในฟอรั่ม,
         และเตือนล่วงหน้าก่อนถึง due date

ไม่ต้องติดตั้งไลบรารีเพิ่ม (stdlib ล้วน)
"""

import argparse
import hashlib
import html
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TH_TZ = timezone(timedelta(hours=7))
UA = "moodle-watch/1.0 (personal course notifier)"


# ---------------------------------------------------------------- config ----
def load_env():
    """อ่าน .env แบบง่าย ๆ (ไม่ทับค่าที่ตั้งไว้ใน environment แล้ว)"""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def keychain_password(service, account):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


class Config:
    def __init__(self):
        load_env()
        self.base = os.environ.get("MOODLE_URL", "https://courses.cs.tu.ac.th").rstrip("/")
        self.user = os.environ.get("MOODLE_USER", "")
        self.password = os.environ.get("MOODLE_PASS", "") or keychain_password(
            os.environ.get("KEYCHAIN_SERVICE", "moodle-watch"), self.user
        ) or ""
        raw_courses = os.environ.get("COURSE_IDS", "all").strip()
        self.course_ids = (
            [] if raw_courses.lower() == "all"
            else [c.strip() for c in raw_courses.split(",") if c.strip()]
        )
        self.auto_discover = not self.course_ids  # COURSE_IDS ว่าง/เป็น "all" → เฝ้าทุกคอร์สที่ลงทะเบียน
        self.state_file = os.environ.get("STATE_FILE", os.path.join(HERE, "state.json"))
        self.discord = os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.macos = os.environ.get("NOTIFY_MACOS", "0") == "1"
        self.ws_service = os.environ.get("WS_SERVICE", "moodle_mobile_app")
        # เตือนล่วงหน้ากี่ชั่วโมงก่อนกำหนดส่ง (คั่นด้วย ,)
        self.due_alerts = [
            int(h) for h in os.environ.get("DUE_ALERT_HOURS", "72,24,6").split(",") if h.strip()
        ]
        self.timeout = int(os.environ.get("HTTP_TIMEOUT", "30"))
        self.insecure = os.environ.get("INSECURE_TLS", "0") == "1"

    def missing(self):
        m = []
        if not self.user:
            m.append("MOODLE_USER")
        if not self.password:
            m.append("MOODLE_PASS (หรือเก็บไว้ใน macOS Keychain)")
        if not (self.discord or self.tg_token or self.macos):
            m.append("ช่องทางแจ้งเตือนอย่างน้อย 1 อย่าง (DISCORD_WEBHOOK_URL / TELEGRAM_* / NOTIFY_MACOS)")
        return m


# ------------------------------------------------------------------ http ----
class Http:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        if cfg.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.opener.addheaders = [("User-Agent", UA)]

    def get(self, url, params=None):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=self.cfg.timeout) as r:
            return r.read().decode("utf-8", "replace"), r.geturl()

    def post(self, url, data, json_body=False):
        if json_body:
            body = json.dumps(data).encode()
            headers = {"Content-Type": "application/json"}
        else:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with self.opener.open(req, timeout=self.cfg.timeout) as r:
            return r.read().decode("utf-8", "replace"), r.geturl()


# --------------------------------------------------------------- helpers ----
def strip_tags(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return "\n".join(l.strip() for l in s.split("\n") if l.strip())


def fp(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def ts_th(epoch):
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), TH_TZ).strftime("%d/%m/%Y %H:%M น.")


def now():
    return int(time.time())


# ------------------------------------------------------- moodle: ws mode ----
class MoodleWS:
    """ดึงข้อมูลผ่าน Moodle Web Service (REST + token)"""

    def __init__(self, cfg, http_, token=None):
        self.cfg = cfg
        self.http = http_
        self.token = token
        self.endpoint = f"{cfg.base}/webservice/rest/server.php"

    def login(self):
        # หมายเหตุ: เซิร์ฟเวอร์นี้ POST ไป /login/token.php แล้ว "ค้าง" (ไม่ตอบกลับ)
        # จึงต้องขอ token ด้วย GET + query string เป็นหลัก แล้วค่อย fallback เป็น POST
        url = f"{self.cfg.base}/login/token.php"
        params = {
            "username": self.cfg.user,
            "password": self.cfg.password,
            "service": self.cfg.ws_service,
        }
        try:
            raw, _ = self.http.get(url, params)
        except Exception as e:
            log(f"  (token.php ผ่าน GET ไม่สำเร็จ: {e} → ลอง POST)")
            raw, _ = self.http.post(url, params)
        data = json.loads(raw)
        if "token" not in data:
            raise RuntimeError(
                f"ขอ token ไม่ได้: {data.get('errorcode')} — {data.get('error')}"
            )
        self.token = data["token"]
        return self.token

    def call(self, function, **params):
        payload = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        for k, v in params.items():
            if isinstance(v, (list, tuple)):
                for i, item in enumerate(v):
                    payload[f"{k}[{i}]"] = item
            else:
                payload[k] = v
        raw, _ = self.http.post(self.endpoint, payload)
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("exception"):
            raise RuntimeError(f"{function}: {data.get('errorcode')} — {data.get('message')}")
        return data

    def discover_course_ids(self):
        """คืนรหัสคอร์สทั้งหมดที่บัญชีนี้ลงทะเบียนอยู่ (ไม่รวมคอร์สหลัก id=1)"""
        info = self.call("core_webservice_get_site_info")
        userid = info.get("userid")
        if not userid:
            raise RuntimeError("หา userid ไม่ได้ (core_webservice_get_site_info)")
        courses = self.call("core_enrol_get_users_courses", userid=userid)
        return [str(c["id"]) for c in courses if str(c.get("id")) not in ("", "1")]

    # ---- snapshot ----
    def snapshot(self, course_id):
        items = {}
        course_name = f"course {course_id}"

        # 1) เนื้อหาคอร์ส: section -> module (ไฟล์ / assignment / quiz / link ...)
        try:
            sections = self.call("core_course_get_contents", courseid=course_id)
        except Exception as e:
            raise RuntimeError(f"อ่านเนื้อหาคอร์สไม่ได้: {e}")

        for sec in sections:
            sec_name = strip_tags(sec.get("name") or "")
            for mod in sec.get("modules", []):
                cmid = mod.get("id")
                files = sorted(
                    (c.get("filename", ""), c.get("filesize", 0))
                    for c in (mod.get("contents") or [])
                    if c.get("type") == "file"
                )
                key = f"mod:{cmid}"
                items[key] = {
                    "kind": "activity",
                    "title": strip_tags(mod.get("name") or ""),
                    "section": sec_name,
                    "modname": mod.get("modname", ""),
                    "url": mod.get("url") or f"{self.cfg.base}/course/view.php?id={course_id}",
                    "detail": ", ".join(f[0] for f in files[:6]),
                    "fp": fp(mod.get("name"), mod.get("visible"), files,
                             strip_tags(mod.get("description") or "")),
                }

        # 2) การบ้าน: กำหนดส่ง / เนื้อหาโจทย์
        try:
            assigns = self.call("mod_assign_get_assignments", courseids=[course_id])
            for c in assigns.get("courses", []):
                course_name = c.get("fullname") or course_name
                for a in c.get("assignments", []):
                    key = f"assign:{a['id']}"
                    items[key] = {
                        "kind": "assignment",
                        "title": strip_tags(a.get("name") or ""),
                        "section": "การบ้าน",
                        "modname": "assign",
                        "url": f"{self.cfg.base}/mod/assign/view.php?id={a.get('cmid')}",
                        "duedate": a.get("duedate") or 0,
                        "detail": (
                            f"กำหนดส่ง {ts_th(a.get('duedate'))}" if a.get("duedate") else "ไม่มีกำหนดส่ง"
                        ),
                        "fp": fp(a.get("name"), a.get("duedate"), a.get("cutoffdate"),
                                 a.get("allowsubmissionsfromdate"),
                                 strip_tags(a.get("intro") or "")),
                    }
        except Exception as e:
            log(f"  (ข้าม assignment: {e})")

        # 3) ประกาศ / กระทู้ในฟอรั่ม
        try:
            forums = self.call("mod_forum_get_forums_by_courses", courseids=[course_id])
            for fm in forums:
                fid = fm.get("id")
                try:
                    res = self.call("mod_forum_get_forum_discussions", forumid=fid, perpage=25)
                except Exception:
                    res = self.call("mod_forum_get_forum_discussions_paginated",
                                    forumid=fid, perpage=25)
                for d in res.get("discussions", []):
                    key = f"disc:{d.get('discussion') or d.get('id')}"
                    body = strip_tags(d.get("message") or "")
                    items[key] = {
                        "kind": "post",
                        "title": strip_tags(d.get("name") or d.get("subject") or ""),
                        "section": strip_tags(fm.get("name") or "ฟอรั่ม"),
                        "modname": "forum",
                        "url": f"{self.cfg.base}/mod/forum/discuss.php?d={key.split(':')[1]}",
                        "detail": (body[:280] + "…") if len(body) > 280 else body,
                        "author": d.get("userfullname", ""),
                        "fp": fp(d.get("name"), d.get("timemodified"), body[:2000]),
                    }
        except Exception as e:
            log(f"  (ข้ามฟอรั่ม: {e})")

        return course_name, items


# --------------------------------------------------- moodle: scrape mode ----
class MoodleHTML:
    """สำรอง: ล็อกอินด้วย session แล้ว diff HTML ของหน้าคอร์ส"""

    def __init__(self, cfg, http_):
        self.cfg = cfg
        self.http = http_

    def login(self):
        page, _ = self.http.get(f"{self.cfg.base}/login/index.php")
        m = re.search(r'name="logintoken"\s+value="([^"]+)"', page)
        token = m.group(1) if m else ""
        body, url = self.http.post(f"{self.cfg.base}/login/index.php", {
            "anchor": "", "logintoken": token,
            "username": self.cfg.user, "password": self.cfg.password,
        })
        if "loginerrors" in body or "Invalid login" in body or "login/index.php" in url:
            raise RuntimeError("ล็อกอินไม่สำเร็จ — ตรวจ MOODLE_USER / MOODLE_PASS")
        return True

    def discover_course_ids(self):
        """คืนรหัสคอร์สทั้งหมดที่บัญชีนี้ลงทะเบียนอยู่ โดยขูดจากหน้า Dashboard"""
        page, url = self.http.get(f"{self.cfg.base}/my/courses.php")
        if "/login/index.php" in url:
            raise RuntimeError("session หลุด (ถูกเด้งไปหน้า login)")
        ids = sorted(set(re.findall(r"/course/view\.php\?id=(\d+)", page)), key=int)
        return [i for i in ids if i != "1"]

    def snapshot(self, course_id):
        page, url = self.http.get(f"{self.cfg.base}/course/view.php?id={course_id}")
        if "/login/index.php" in url:
            raise RuntimeError("session หลุด (ถูกเด้งไปหน้า login)")
        if "/enrol/index.php" in url:
            raise RuntimeError(f"บัญชีนี้ยังไม่ได้ลงทะเบียนในคอร์ส {course_id}")

        m = re.search(r"<title>(.*?)</title>", page, re.S)
        course_name = strip_tags(m.group(1)) if m else f"course {course_id}"

        items = {}
        # ตัดหน้าเว็บเป็นก้อน ๆ ตาม id="module-XXXX"
        chunks = re.split(r'id="module-(\d+)"', page)
        for i in range(1, len(chunks) - 1, 2):
            cmid, chunk = chunks[i], chunks[i + 1]
            name_m = re.search(r'class="instancename">(.*?)</span>', chunk, re.S)
            title = strip_tags(name_m.group(1)) if name_m else ""
            title = re.sub(r"\s+(Assignment|File|URL|Forum|Quiz|Page|Folder|ไฟล์|แบบทดสอบ)$", "", title).strip()
            href_m = re.search(r'href="(https?://[^"]*?/mod/([a-z_]+)/view\.php[^"]*)"', chunk)
            url_ = html.unescape(href_m.group(1)) if href_m else f"{self.cfg.base}/course/view.php?id={course_id}"
            modname = href_m.group(2) if href_m else "unknown"
            text = strip_tags(chunk)[:1200]
            if not title:
                continue
            items[f"mod:{cmid}"] = {
                "kind": "activity", "title": title, "section": "",
                "modname": modname, "url": url_,
                "detail": " / ".join(text.split("\n")[1:3])[:200],
                "fp": fp(title, text),
            }

        # ฟอรั่ม: ตามเข้าไปดูรายชื่อกระทู้
        for key, it in list(items.items()):
            if it["modname"] != "forum":
                continue
            try:
                fpage, _ = self.http.get(it["url"])
            except Exception:
                continue
            for d_id, subj in re.findall(
                r'href="[^"]*?/mod/forum/discuss\.php\?d=(\d+)[^"]*"[^>]*>(.*?)</a>', fpage, re.S
            ):
                subj = strip_tags(subj)
                if not subj:
                    continue
                items[f"disc:{d_id}"] = {
                    "kind": "post", "title": subj, "section": it["title"],
                    "modname": "forum",
                    "url": f"{self.cfg.base}/mod/forum/discuss.php?d={d_id}",
                    "detail": "", "fp": fp(subj),
                }
        return course_name, items


# ----------------------------------------------------------------- diff -----
KIND_LABEL = {
    "activity": "กิจกรรม/ไฟล์",
    "assignment": "การบ้าน",
    "post": "ประกาศ/กระทู้",
}
MOD_ICON = {
    "assign": "📝", "quiz": "🧪", "resource": "📄", "folder": "📁", "url": "🔗",
    "forum": "📢", "page": "📃", "label": "🏷️", "feedback": "🗳️", "workshop": "🛠️",
}


def diff(old, new):
    events = []
    for k, v in new.items():
        if k not in old:
            events.append(("new", k, v, None))
        elif old[k].get("fp") != v.get("fp"):
            events.append(("changed", k, v, old[k]))
    for k, v in old.items():
        if k not in new:
            events.append(("removed", k, v, None))
    order = {"new": 0, "changed": 1, "removed": 2}
    events.sort(key=lambda e: (order[e[0]], e[2].get("kind", "")))
    return events


def due_reminders(items, sent, alerts):
    """คืนรายการเตือนก่อนกำหนดส่ง (แจ้งครั้งเดียวต่อ 1 ช่วงเวลา)"""
    out = []
    t = now()
    for k, v in items.items():
        due = int(v.get("duedate") or 0)
        if not due or due <= t:
            continue
        left_h = (due - t) / 3600.0
        for h in sorted(alerts):
            if left_h <= h:
                marker = f"{k}@{h}"
                if marker not in sent:
                    sent[marker] = t
                    out.append((h, v))
                break
    return out


# --------------------------------------------------------------- notify -----
def fmt_event(action, item):
    icon = MOD_ICON.get(item.get("modname", ""), "•")
    verb = {"new": "🆕 ใหม่", "changed": "✏️ แก้ไข", "removed": "🗑️ ถูกลบ"}[action]
    lines = [f"{verb} · {icon} **{item.get('title','(ไม่มีชื่อ)')}**"]
    meta = []
    if item.get("section"):
        meta.append(f"📂 {item['section']}")
    if item.get("author"):
        meta.append(f"✍️ {item['author']}")
    if meta:
        lines.append("> " + "  ·  ".join(meta))
    for l in item.get("detail", "").split("\n"):
        if l.strip():
            lines.append(f"> {l.strip()}")
    if action != "removed" and item.get("url"):
        lines.append(f"> 🔗 {item['url']}")
    return "\n".join(lines)


def fmt_due(hours_left, item):
    lines = [f"⏰ **ใกล้ถึงกำหนดส่ง** (อีก ~{hours_left} ชม.) · {item.get('title','(ไม่มีชื่อ)')}"]
    if item.get("detail"):
        lines.append(f"> {item['detail']}")
    if item.get("url"):
        lines.append(f"> 🔗 {item['url']}")
    return "\n".join(lines)


def send_discord(webhook, title, body, http_):
    for chunk in split_chunks(body, 1800):
        http_.post(webhook, {"content": f"**{title}**\n{chunk}", "flags": 4}, json_body=True)


def send_telegram(token, chat, title, body, http_):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_chunks(body, 3500):
        http_.post(url, {
            "chat_id": chat,
            "text": f"{title}\n\n{chunk}",
            "disable_web_page_preview": "true",
        })


def send_macos(title, body):
    # ส่งข้อความผ่าน argv เพื่อไม่ต้อง escape — รองรับภาษาไทยและอักขระพิเศษ
    subprocess.run([
        "osascript",
        "-e", "on run argv",
        "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
        "-e", "end run",
        body[:220], title[:120],
    ], check=False)


def split_chunks(text, limit):
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out or [text]


def notify(cfg, http_, title, body):
    ok = False
    if cfg.discord:
        try:
            send_discord(cfg.discord, title, body, http_)
            ok = True
        except Exception as e:
            log(f"  ! Discord ล้มเหลว: {e}")
    if cfg.tg_token and cfg.tg_chat:
        try:
            send_telegram(cfg.tg_token, cfg.tg_chat, title, body, http_)
            ok = True
        except Exception as e:
            log(f"  ! Telegram ล้มเหลว: {e}")
    if cfg.macos:
        send_macos(title, body.replace("\n", " "))
        ok = True
    return ok


# ---------------------------------------------------------------- state -----
def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"courses": {}, "due_sent": {}, "token": ""}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def log(msg):
    print(f"[{datetime.now(TH_TZ):%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------- main -----
def build_client(cfg, http_, state, force_mode=None):
    """คืน (client, mode) — พยายามใช้ Web Service ก่อน"""
    if force_mode != "html":
        ws = MoodleWS(cfg, http_, token=state.get("token") or None)
        try:
            if ws.token:
                ws.call("core_webservice_get_site_info")   # เช็คว่า token ยังใช้ได้
            else:
                ws.login()
            state["token"] = ws.token
            return ws, "ws"
        except Exception as e:
            log(f"โหมด Web Service ใช้ไม่ได้ ({e}) → สลับไปโหมด HTML")
            state["token"] = ""
            if force_mode == "ws":
                raise
    scraper = MoodleHTML(cfg, http_)
    scraper.login()
    return scraper, "html"


def run_once(cfg, state, init=False, force_mode=None):
    http_ = Http(cfg)
    client, mode = build_client(cfg, http_, state, force_mode)
    log(f"เชื่อมต่อสำเร็จ (โหมด {mode.upper()})")

    course_ids = cfg.course_ids
    if cfg.auto_discover:
        try:
            course_ids = client.discover_course_ids()
            log(f"เฝ้าทุกคอร์สที่ลงทะเบียน: {len(course_ids)} คอร์ส ({', '.join(course_ids) or '-'})")
        except Exception as e:
            course_ids = list(state["courses"].keys())
            if not course_ids:
                raise
            log(f"หารายชื่อคอร์สอัตโนมัติไม่ได้ ({e}) — ใช้รายชื่อคอร์สที่เคยเจอล่าสุดแทน: {', '.join(course_ids)}")

    total = 0
    for cid in course_ids:
        try:
            course_name, items = client.snapshot(cid)
        except Exception as e:
            log(f"คอร์ส {cid}: ดึงข้อมูลไม่ได้ — {e}")
            continue

        prev = state["courses"].get(cid, {}).get("items", {})
        first_run = not prev

        events = [] if (init or first_run) else diff(prev, items)
        dues = [] if (init or first_run) else due_reminders(
            items, state.setdefault("due_sent", {}), cfg.due_alerts
        )

        state["courses"][cid] = {
            "name": course_name,
            "items": items,
            "checked_at": now(),
        }

        if init or first_run:
            log(f"คอร์ส {cid} ({course_name}): บันทึกฐานข้อมูลตั้งต้น {len(items)} รายการ (ยังไม่แจ้งเตือน)")
            continue

        if not events and not dues:
            log(f"คอร์ส {cid} ({course_name}): ไม่มีอะไรใหม่ ({len(items)} รายการ)")
            continue

        parts = [fmt_event(a, it) for a, _, it, _ in events]
        parts += [fmt_due(h, it) for h, it in dues]
        body = "\n\n".join(parts)
        title = f"📚 {course_name} — มีอัปเดต {len(events) + len(dues)} รายการ"
        log(f"คอร์ส {cid}: พบ {len(events)} การเปลี่ยนแปลง, {len(dues)} เตือนกำหนดส่ง")
        if notify(cfg, http_, title, body):
            total += len(events) + len(dues)
        else:
            log("  ! ส่งแจ้งเตือนไม่สำเร็จเลยสักช่องทาง — จะไม่บันทึก state เพื่อลองใหม่รอบหน้า")
            state["courses"][cid]["items"] = prev
    return total


def main():
    ap = argparse.ArgumentParser(description="แจ้งเตือนเมื่อ Moodle มีงาน/ประกาศใหม่")
    ap.add_argument("--init", action="store_true",
                    help="บันทึกสถานะปัจจุบันเป็นฐานตั้งต้น (ไม่ส่งแจ้งเตือน) — รันครั้งแรก")
    ap.add_argument("--test-notify", action="store_true", help="ทดสอบส่งแจ้งเตือน")
    ap.add_argument("--mode", choices=["auto", "ws", "html"], default="auto",
                    help="บังคับโหมดการดึงข้อมูล")
    ap.add_argument("--loop", type=int, metavar="MINUTES",
                    help="รันวนซ้ำทุก N นาที (ค้างไว้ในเทอร์มินัล)")
    args = ap.parse_args()

    cfg = Config()
    if args.test_notify:
        http_ = Http(cfg)
        ok = notify(cfg, http_, "✅ ทดสอบ moodle-watch",
                    "ถ้าเห็นข้อความนี้ แปลว่าช่องทางแจ้งเตือนพร้อมใช้งานแล้ว")
        print("ส่งสำเร็จ" if ok else "ส่งไม่สำเร็จ — ตรวจการตั้งค่าช่องทางแจ้งเตือน")
        return 0 if ok else 1

    missing = cfg.missing()
    if missing:
        print("ยังตั้งค่าไม่ครบ:\n  - " + "\n  - ".join(missing))
        print(f"\nแก้ที่ {os.path.join(HERE, '.env')} (ดูตัวอย่างใน .env.example)")
        return 2

    force = None if args.mode == "auto" else args.mode
    while True:
        state = load_state(cfg.state_file)
        try:
            run_once(cfg, state, init=args.init, force_mode=force)
        except Exception as e:
            log(f"ผิดพลาด: {e}")
        save_state(cfg.state_file, state)
        if not args.loop:
            break
        time.sleep(max(60, args.loop * 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
