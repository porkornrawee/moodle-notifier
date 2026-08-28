# moodle-notifier — แจ้งเตือนเมื่ออาจารย์โพสงานบน courses.cs.tu.ac.th

เฝ้าดู**ทุกคอร์ส**ที่บัญชีคุณลงทะเบียนอยู่บน Moodle ของ CS TU (หรือจะระบุเฉพาะบางคอร์สก็ได้)
ทุก ๆ 15 นาที ถ้ามีอะไรเปลี่ยน → ส่งแจ้งเตือนเข้า Discord / Telegram / Notification Center ของ Mac

## ตรวจจับอะไรได้บ้าง

| เหตุการณ์ | ตัวอย่าง |
|---|---|
| 🆕 กิจกรรม/ไฟล์ใหม่ | อาจารย์อัปสไลด์ บทที่ 5, สร้าง Assignment ใหม่ |
| ✏️ ของเดิมถูกแก้ | เปลี่ยนชื่องาน, **เลื่อนกำหนดส่ง**, แก้โจทย์, เพิ่มไฟล์แนบ |
| 📢 ประกาศ/กระทู้ใหม่ | โพสต์ใน Announcements หรือฟอรั่มใด ๆ ในคอร์ส |
| 🗑️ ของถูกลบ | กิจกรรมหายไปจากหน้าคอร์ส |
| ⏰ ใกล้ถึงกำหนดส่ง | เตือนล่วงหน้า 72 / 24 / 6 ชม. (ปรับได้) |

ไม่ต้อง `pip install` อะไรเลย — ใช้ Python stdlib ล้วน

---

## ติดตั้ง (5 นาที)

### 1. ตั้งค่า

```bash
cd ~/Desktop/moodle-notifier
cp .env.example .env
open -e .env          # ใส่ username / password Moodle + ช่องทางแจ้งเตือน
```

ถ้าไม่อยากเก็บรหัสผ่านเป็นไฟล์ ให้เก็บใน macOS Keychain แทน แล้วลบบรรทัด `MOODLE_PASS` ทิ้ง:

```bash
security add-generic-password -s moodle-watch -a <username_moodle_ของคุณ> -w
```

ค่าเริ่มต้น `COURSE_IDS=all` จะเฝ้า**ทุกคอร์สที่บัญชีนี้ลงทะเบียนอยู่โดยอัตโนมัติ**
(เช็คซ้ำทุกรอบ ถ้าลงทะเบียนคอร์สใหม่ระหว่างเทอมก็เพิ่มให้เองโดยไม่ต้องแก้ค่าอะไร)
อยากเฝ้าเฉพาะบางคอร์สก็ระบุรหัสคอร์สแทนได้ เช่น `COURSE_IDS=914,1023`

### 2. เลือกช่องทางแจ้งเตือน (เปิดกี่ช่องทางพร้อมกันก็ได้)

- **Discord** (แนะนำ): Server Settings → Integrations → Webhooks → New Webhook → Copy URL → ใส่ `DISCORD_WEBHOOK_URL`
- **Telegram**: ทัก `@BotFather` → `/newbot` → ได้ token → ทักบอทตัวเอง 1 ครั้ง → เปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` เพื่อดู chat id
- **Mac notification**: `NOTIFY_MACOS=1` (เด้งบนหน้าจอ ใช้ได้เฉพาะตอนรันบนเครื่องตัวเอง)

> ⚠️ **LINE Notify ใช้ไม่ได้แล้ว** — LINE ปิดบริการไปตั้งแต่ 31 มี.ค. 2025
> ถ้าอยากได้เข้า LINE จริง ๆ ต้องทำ LINE Official Account + Messaging API (ยุ่งกว่ามาก) — Discord/Telegram ง่ายกว่าเยอะ

### 3. ทดสอบ

```bash
python3 moodle_watch.py --test-notify    # เช็คว่าช่องทางแจ้งเตือนใช้ได้
python3 moodle_watch.py --init           # บันทึกสถานะปัจจุบันเป็นฐานตั้งต้น (ไม่สแปม)
python3 moodle_watch.py                  # เช็ค 1 รอบ
```

`--init` สำคัญ: ถ้าไม่รัน รอบแรกจะเห็นทุกอย่างเป็น "ของใหม่" ทั้งหมด
(จริง ๆ สคริปต์กันไว้ให้แล้ว — รอบแรกที่ยังไม่มี state จะไม่ส่งแจ้งเตือน)

### 4. ให้รันอัตโนมัติ

**บน Mac (launchd, ทุก 15 นาที):**

```bash
cp launchd/com.moodlewatch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.moodlewatch.plist
tail -f moodle-watch.log            # ดูว่าทำงานไหม
# ถอนออก: launchctl unload ~/Library/LaunchAgents/com.moodlewatch.plist
```

**หรือรันค้างในเทอร์มินัลเฉย ๆ:** `python3 moodle_watch.py --loop 15`

**บน GitHub Actions (ทำงาน 24 ชม. แม้ปิดเครื่อง):**
push โฟลเดอร์นี้ขึ้น repo **private** แล้วตั้ง Secrets: `MOODLE_USER`, `MOODLE_PASS`,
`DISCORD_WEBHOOK_URL` (หรือ `TELEGRAM_*`) — workflow อยู่ที่ `.github/workflows/moodle-watch.yml`

---

## ทำงานยังไง

สคริปต์ลองสองโหมด เรียงตามความแม่นยำ:

1. **Web Service API** — ขอ token จาก `login/token.php` แล้วเรียก `core_course_get_contents`,
   `mod_assign_get_assignments`, `mod_forum_get_forum_discussions`
   ได้ข้อมูลมีโครงสร้าง รู้ทั้งกำหนดส่งและชื่อไฟล์แนบ
2. **HTML scraping** (สำรอง) — ล็อกอินด้วย session ปกติ แล้ว diff หน้าคอร์ส
   ใช้เมื่อ service `moodle_mobile_app` ถูกปิด

ทุกรอบจะเทียบ "ลายนิ้วมือ" (hash) ของแต่ละรายการกับ `state.json` → มีอะไรต่างค่อยแจ้ง
ถ้าส่งแจ้งเตือนไม่สำเร็จ จะ **ไม่** บันทึก state เพื่อให้ลองใหม่รอบหน้า (ไม่มีทางพลาดงาน)

---

## เงื่อนไข / ข้อจำกัด ที่ต้องรู้

| เรื่อง | รายละเอียด |
|---|---|
| **ต้องมีบัญชีของคุณเอง** | หน้าคอร์สเด้งไป login ถ้าไม่ล็อกอิน และเด้งไป `enrol/index.php` ถ้ายังไม่ได้ลงทะเบียนวิชานั้น — สคริปต์ใช้บัญชีคุณ ดูได้เท่าที่คุณเห็น |
| **รหัสผ่านอยู่ในเครื่องคุณ** | เก็บใน `.env` (มี `.gitignore` แล้ว) หรือ Keychain — ไม่ถูกส่งไปที่ไหนนอกจาก courses.cs.tu.ac.th |
| **เซิร์ฟเวอร์เก่า** | Apache 2.2 / PHP 7.0 — `POST /login/token.php` **ค้างไม่ตอบ** สคริปต์เลยขอ token ด้วย GET แทน (แก้ไว้แล้ว) |
| **มีระบบกันยิงถี่** | ทดสอบล็อกอินผิดติด ๆ กันแล้วเจอ connection reset → **อย่าตั้งต่ำกว่า 10 นาที** และอย่าใส่รหัสผิดซ้ำ ๆ (Moodle มี account lockout ด้วย) ค่าเริ่มต้น 15 นาที กำลังดี |
| **ไม่ใช่ real-time** | รู้ช้าสุด ~15 นาทีหลังอาจารย์โพสต์ ถ้าอยากไวขึ้นลดเหลือ 10 นาทีได้ ต่ำกว่านั้นไม่แนะนำ |
| **ปิดเครื่อง = ไม่เตือน** | โหมด launchd ทำงานเฉพาะตอน Mac เปิดอยู่ (มันจะรันชดเชยให้ 1 ครั้งตอนเปิดเครื่อง) ถ้าอยากได้ 24 ชม. ใช้ GitHub Actions |
| **ถ้ามหาลัยเปลี่ยนไปใช้ SSO/2FA** | สคริปต์จะล็อกอินไม่ได้ ต้องเปลี่ยนไปใช้วิธี copy cookie แทน (แจ้งได้ เดี๋ยวแก้ให้) |
| **ทางเลือกที่ "ถูกกติกา" ที่สุด** | Moodle มีของแถมอยู่แล้ว: กด **Subscribe** ในฟอรั่ม Announcements (ได้อีเมล), Calendar → **Export** เป็น iCal แล้ว subscribe ใน Google Calendar, และแอป **Moodle Mobile** ก็มี push แต่ทั้งหมดนี้ไม่ครอบคลุมเวลาอาจารย์แค่อัปไฟล์เงียบ ๆ ซึ่งเป็นจุดที่สคริปต์นี้ชนะ |

## แก้ปัญหา

```bash
python3 moodle_watch.py --mode ws      # บังคับใช้ Web Service API
python3 moodle_watch.py --mode html    # บังคับใช้การขูดหน้าเว็บ
rm state.json && python3 moodle_watch.py --init   # รีเซ็ตสถานะใหม่
```

- `invalidlogin` → username/password ผิด หรือโดน lockout (รอ ~30 นาที)
- `accessexception` / `invalidtoken` → service `moodle_mobile_app` ปิดอยู่ ให้ใช้ `--mode html`
- `บัญชีนี้ยังไม่ได้ลงทะเบียนในคอร์ส` → ต้องเข้าคอร์สนั้นก่อน (enrolment key)
