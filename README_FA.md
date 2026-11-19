<div dir="rtl">

<div align="center">

# ارکستراتور محلی پروژه‌های هوش مصنوعی (<bdi>LAPO</bdi>)

**تبدیل مدل‌های زبانی محلی (<bdi>Strata</bdi>، <bdi>llama.cpp</bdi>، <bdi>LM Studio</bdi> و <bdi>vLLM</bdi>) به عامل مهندسی چندفازه، خودکار و بدون توقف**

<br>

[صفحه اصلی (<bdi>README.md</bdi>)](README.md) &nbsp;•&nbsp;
[**English Docs (<bdi>README_EN.md</bdi>)**](README_EN.md) &nbsp;•&nbsp;
[مدل امنیتی (<bdi>SECURITY.md</bdi>)](SECURITY.md) &nbsp;•&nbsp;
[تغییرات (<bdi>CHANGELOG.md</bdi>)](CHANGELOG.md)

</div>

---

## ۱. معرفی پروژه

**ارکستراتور محلی پروژه‌های هوش مصنوعی** (<bdi>Local AI Project Orchestrator</bdi>) یک سیستم نظارتی و اجرای خودکار با پایتون ۳.۱۰ به بالا برای ویندوز و لینوکس است که برای مدیریت پروژه‌های طولانی و چندمرحله‌ای روی مدل‌های محلی طراحی شده است.

این ابزار یک چت‌بات ساده نیست؛ بلکه به‌عنوان ناظر مهندسی بین کاربر و مدل قرار می‌گیرد و موارد زیر را به‌صورت خودکار مدیریت می‌کند:

- اجرای مرحله‌به‌مرحله و منظم پروژه بر اساس فازهای تعریف‌شده توسط کاربر
- دسترسی کنترل‌شده و امن به فایل‌های پوشه‌ی کاری (<bdi>Workspace</bdi>) و پوشه‌ی فقط‌خواندنی راهنماها (<bdi>Skills</bdi>)
- تست‌گیری خودکار در پایان هر فاز بدون نیاز به حضور کاربر پای سیستم
- فشرده‌سازی و صفر کردن کانتکست هنگام پر شدن حافظه بدون باطل کردن <bdi>KV-Cache</bdi> سرور
- قفل‌گذاری فایل‌های نهایی هر فاز با اثر انگشت <bdi>SHA-256</bdi>
- دریافت دستورات جدید کاربر در میانه‌ی اجرا بدون توقف فرآیند
- بازیابی دقیق نشست پس از کرش یا قطع برق و گزارش کامل مصرف توکن‌ها

---

## ۲. ویژگی‌های کلیدی

| بخش | عملکرد در <bdi>LAPO</bdi> |
| :--- | :--- |
| **یک هسته مشترک (<bdi>GUI + CLI</bdi>)** | رابط دسکتاپ (<bdi>Tkinter</bdi>) و خط فرمان بدون گرافیک (<bdi>lapo</bdi>) هر دو دقیقاً از یک موتور واحد در مسیر `lapo/core/` استفاده می‌کنند. |
| **اتصال داینامیک و کشف مدل‌ها** | بدون هیچ پورت هاردکدشده؛ هر آدرس و پورتی را وارد کنید تا لیست مدل‌ها با برچسب‌های `[ACTIVE]`، `[INSTALLED - NOT LOADED]` و `[API ONLY]` نمایش داده شود. |
| **حالت تمام‌خودکار (<bdi>Unattended Mode</bdi>)** | به صورت پیش‌فرض فعال است؛ در نبود دستور تست دستی، خودش سینتکس و تست‌های پروژه (`unittest`، `tsc`، `vitest`، `go test`، `cargo check`) را اجرا می‌کند و پس از موفقیت، فاز را قفل کرده و به فاز بعدی می‌رود. |
| **فشرده‌سازی بدون شکستن <bdi>KV-Cache</bdi>** | شمارش دقیق توکن‌ها با `POST /tokenize`؛ هنگام رسیدن به آستانه، ابزارها در سمت پایتون قفل می‌شوند اما اسکیمای ابزارها از درخواست حذف نمی‌شود تا کش سرور حفظ شود؛ پس از خلاصه‌سازی، کانتکست صفر شده و کار دقیقاً در همان فاز فعال ادامه می‌یابد. |
| **پروتکل دوگانه نوشتن فایل** | علاوه بر ابزارهای `write_file` و `patch_file`، بلوک‌های مستقیم `<file path="...">...</file>` را از متن پاسخ مدل استخراج می‌کند تا مشکل بریده شدن <bdi>JSON</bdi> در فایل‌های طولانی کاملاً حل شود. |
| **سازگار با شرایط واقعی مدل‌های محلی** | بای‌پس خودکار پروکسی سیستم و <bdi>VPN</bdi> برای آدرس‌های `127.0.0.1` و `localhost`، دیکودینگ صریح <bdi>UTF-8</bdi> در استریم <bdi>SSE</bdi> برای نمایش صحیح فارسی، و تایم‌اوت طولانی (پیش‌فرض ۹۰ دقیقه) برای مدل‌های سنگین <bdi>MoE</bdi>. |
| **دستور زنده در میانه کار** | ناظر پس‌زمینه فایل `user_request.json` را به‌صورت لحظه‌ای می‌خواند، به فاز هدف هدایت می‌کند و بلافاصله نام آن را به `readed_user_request_<timestamp>.json` تغییر می‌دهد تا دوباره خوانده نشود. |
| **قفل فایل و تعمیر کنترل‌شده** | فایل‌های نهایی با هش <bdi>SHA-256</bdi> قفل می‌شوند و مدل حق تغییر آن‌ها را ندارد؛ فقط با مجوز کاربر (<bdi>Unlock for Repair</bdi>) امکان اصلاح و تست مجدد فاز مالک آن‌ها فراهم می‌شود. |
| **گزارش دقیق توکن و <bdi>Unload</bdi>** | تفکیک دقیق توکن‌های خواندن (<bdi>Read</bdi>)، تفکر (<bdi>Think</bdi>) و تولید (<bdi>Write</bdi>) بدون شمارش مضاعف، ذخیره گزارش <bdi>JSON</bdi> و <bdi>Markdown</bdi>، و ارسال دستور <bdi>Unload</bdi> پس از تایید نهایی کاربر. |

---

## ۳. نصب و راه‌اندازی

### پیش‌نیازها
- **پایتون ۳.۱۰ یا جدیدتر**
- یک سرور استنتاج محلی در حال اجرا (مانند <bdi>Strata</bdi>، <bdi>llama.cpp</bdi> یا <bdi>LM Studio</bdi>)
- در لینوکس دبیان و اوبونتو (صرفاً برای محیط گرافیکی): نصب بسته‌ی `python3-tk`

### روش ۱: اجرای سریع با لانچر خودکار (پیشنهادی)
لانچرها به‌صورت خودکار نسخه‌ی پایتون را بررسی می‌کنند، محیط مجازی `.venv` می‌سازند، وابستگی‌ها را نصب کرده و برنامه را اجرا می‌کنند:

**در ویندوز:** روی فایل `run_windows.bat` دوبار کلیک کنید یا در <bdi>PowerShell</bdi> اجرا کنید:

<div dir="ltr">

```powershell
.\run_windows.bat
```

</div>

**در لینوکس:**

<div dir="ltr">

```bash
chmod +x run_linux.sh
./run_linux.sh
```

</div>

### روش ۲: نصب دستی و فعال‌سازی دستور <bdi>lapo</bdi>

<div dir="ltr">

```bash
python -m venv .venv

# Windows:
.venv\Scripts\activate

# Linux:
source .venv/bin/activate

pip install -r requirements.txt
pip install .

# Launch GUI:
lapo

# Show CLI help:
lapo --help
```

</div>

---

## ۴. راهنمای سریع رابط گرافیکی (<bdi>Desktop GUI</bdi>)

1. **تنظیم سرور و انتخاب مدل:** در کادر `API URL` آدرس و پورت سرور محلی خود را وارد کنید (مثلاً `http://127.0.0.1:47831/v1`)، دکمه‌ی **Fetch Models** را بزنید و مدل فعال را از منوی کشویی انتخاب کنید.
2. **مسیر پروژه و <bdi>Skills</bdi>:** پوشه‌ی کاری پروژه (`Workspace`) و در صورت نیاز پوشه‌ی فقط‌خواندنی راهنماها (`Skills`) را انتخاب کنید.
3. **تنظیمات کانتکست و اجرای بدون توقف:** کانتکست را روی `auto` (تشخیص خودکار ظرفیت سرور) یا عدد دلخواه (`32K` تا `1M`) بگذارید و تیک **Auto-Verify & Continue Between Phases** را روشن نگه دارید تا برنامه در غیاب شما متوقف نشود.
4. **تعریف فازها:** در بخش **Phase Builder** فازهای پروژه را با دکمه‌ی **+ Add Phase** اضافه کنید، پرامپت و در صورت تمایل دستورات تست هر فاز را بنویسید و در پایان روی **Finish Phases Input** کلیک کنید.
5. **شروع و مانیتور زنده:** دکمه‌ی **Start** را بزنید. روند تفکر مدل در تب **Thinking Stream**، متن خروجی در **Generated Output** و وضعیت ابزارها، فشرده‌سازی کانتکست و تست‌ها در تب **Activity** به‌صورت آنلاین نمایش داده می‌شود.
6. **ارسال دستور زنده حین اجرا:** هر زمان حین کار مدل نیاز به اصلاح یا تذکر داشتید، فاز هدف را انتخاب کرده و متن را در بخش **Live Mid-Flight User Request** ارسال کنید.
7. **بررسی نهایی و آزادسازی حافظه:** پس از اتمام تمام فازها، گزارش کامل زمان اجرا و مصرف توکن‌ها نمایش داده می‌شود و در صورت تایید شما، دستور <bdi>Unload</bdi> برای آزاد کردن حافظه گرافیکی به سرور ارسال می‌گردد.

---

## ۵. دستورات خط فرمان (<bdi>CLI</bdi>) برای ویندوز و سرورهای لینوکس

<div dir="ltr">

```bash
# List installed Strata models & active API models
lapo models --url [http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1)

# Generate a starter project.json configuration
lapo init-config --directory ./my_project

# Start an interactive multi-phase project run
lapo run --workspace ./my_workspace --url [http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --model my-model --interactive-phases

# Run directly from a saved JSON config
lapo run --config ./examples/project.example.json

# Resume an interrupted session after crash or power loss
lapo resume --workspace ./my_workspace --url [http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --model my-model

# Inject a live user instruction targeting Phase 2
lapo request --workspace ./my_workspace --phase 2 --prompt "Add edge-case validation"

# Inspect session status and SHA-256 lock integrity
lapo status --workspace ./my_workspace

# Authorize controlled repair on a locked file
lapo unlock --workspace ./my_workspace --file src/core.py --reason "Fix boundary condition"

# Verify live Strata server connectivity with a smoke test
lapo run --workspace ./my_workspace --url [http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --smoke-test
```

</div>

---

## ۶. اجرای تست‌های خودکار

برای اطمینان از سلامت کامل موتور، ۲۲ تست واحد و تست یکپارچه (با سرور شبیه‌سازی‌شده‌ی `MockStrataServer`) در پروژه قرار داده شده است:

<div dir="ltr">

```bash
python -m unittest discover -s tests -v
```

</div>

---

## ۷. عیب‌یابی سریع

| مشکل | راه‌حل |
| :--- | :--- |
| **عدم اتصال به `127.0.0.1` هنگام روشن بودن <bdi>VPN</bdi>** | برنامه به‌صورت خودکار پروکسی را برای لوکال‌هاست بای‌پس می‌کند؛ صرفاً مطمئن شوید سرور مدل روی همان پورتی که در `API URL` وارد کرده‌اید در حال اجراست. |
| **نمایش برچسب `[INSTALLED - NOT LOADED]`** | ابتدا مدل را داخل <bdi>Strata</bdi> لود کنید و سپس دوباره روی **Fetch Models** کلیک کنید تا وضعیت آن به `[ACTIVE]` تغییر کند. |
| **خطای اجرای گرافیکی در سرور لینوکس بدون مانیتور** | از دستورات خط فرمان (`./run_linux.sh run --config ...`) استفاده کنید؛ محیط گرافیکی نیازمند <bdi>DISPLAY</bdi> و بسته‌ی `python3-tk` است. |
| **عدم تایید خودکار <bdi>Unload</bdi>** | برخی سرورهای سازگار با <bdi>OpenAI</bdi> اندپوینت <bdi>Unload</bdi> ندارند. برنامه هرگز پیام موفقیت جعلی نشان نمی‌دهد؛ در این حالت مدل را مستقیماً از پنل سرور خود <bdi>Unload</bdi> کنید. |

</div>