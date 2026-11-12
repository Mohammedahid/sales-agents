# وكلاء المبيعات (سين + Nvolve) وبوت تلجرام

## شو بيعمل النظام
- **رقم سين (واتساب):** موظف مبيعات باسم وبلهجة سعودية، وبيحكي بس عن سين.
- **رقم Nvolve (واتساب):** نفس الإشي، بس بيحكي بس عن Nvolve.
- **التحويل لموظف:** إذا العميل طلب حدا، أو بده عرض سعر أو عقد، أو عنده شكوى، البوت بيبطل يرد على هالعميل وبيبعتلك تنبيه على تلجرام. بعدها:
  - بترد من تطبيق واتساب بزنس على جوالك، والبوت بيلاحظ إنك رديت وبيضل ساكت 12 ساعة، أو
  - بتعمل **Reply** على التنبيه بتلجرام، ورسالتك بتوصل للعميل من نفس الرقم.
  - زر **"رجّع البوت يرد"** بيرجّعه يرد على العميل.
- **بوت تلجرام إلك:** `/status` و `/report` و `/leads` و `/handoffs` و `/paused`، أو اسأله أي سؤال بالعربي عن المنتجين.
- كل 5 دقايق بيفحص المواقع، وإذا وقع واحد بيبعتلك تنبيه. وكل يوم الساعة 9 الصبح بيبعتلك تقرير.

## التشغيل على السيرفر

### 1) ارفع الملف على السيرفر (من جهازك)
```
scp -i ssh-key.key sales-agents.tar.gz ubuntu@IP_السيرفر:~
```

### 2) على السيرفر
```
tar xzf sales-agents.tar.gz && cd sales-agents
cp .env.example .env
nano .env
```
عبّي الـ `.env`: الـ DOMAIN، ومفتاح Anthropic، وتوكن تلجرام، ورقمك. خانات واتساب بتضل فاضية هلأ. للحفظ اضغط Ctrl+O ثم Enter، وللخروج Ctrl+X.

### 3) التشغيل
```
docker compose up -d --build
docker compose logs -f app
```
افتح البوت بتلجرام واكتبله `/status`.

### 4) معلومات المنتجات
ارفع ملفات البروفايل PDF لمجلد `knowledge/`، وبعدها شغّل:
```
docker compose run --rm app python tools/build_knowledge.py seen https://موقع-سين knowledge/seen-profile.pdf
docker compose run --rm app python tools/build_knowledge.py nvolve https://موقع-nvolve knowledge/nvolve-profile.pdf
nano knowledge/seen.md
```
افتح الملفات وعدّل الملخص اللي فوق: الأسعار والباقات والأسئلة المتكررة. **البوت ما بيحكي إلا باللي موجود هون.** أي تعديل بيشتغل فوراً بدون ما تعيد التشغيل.

### 5) واتساب (آخر خطوة)
- رابط الـ Webhook: `https://DOMAIN/webhook/whatsapp`
- الـ Verify token هو نفسه `WA_VERIFY_TOKEN`
- اشترك بالحقول: `messages` و `smb_message_echoes` (للـ Coexistence)
- حط `SEEN_PHONE_NUMBER_ID` و `NVOLVE_PHONE_NUMBER_ID` و `WA_ACCESS_TOKEN` و `WA_APP_SECRET`، وبعدها شغّل `docker compose up -d`

## أوامر مفيدة
- السجلات: `docker compose logs -f app`
- إعادة التشغيل بعد تعديل `.env`: `docker compose up -d`
- تحديث الكود: `docker compose up -d --build`
- قاعدة البيانات موجودة في `data/agents.db`
