# BloodNet

BloodNet یک سامانهٔ محلی برای ثبت و پیگیری درخواست فرآورده‌های خونی در شبیه‌ساز است. برنامه درخواست بیمارستان را ثبت می‌کند، سازگاری و موجودی را بررسی می‌کند، گردش اهداکننده و ارسال موجودی را پی می‌گیرد و وضعیت را در یک داشبورد فارسی نمایش می‌دهد.

تمام داده‌های بستهٔ تمرین ساختگی‌اند. این نرم‌افزار ابزار آموزشی و شبیه‌سازی است؛ برای تصمیم‌گیری درمانی یا استفاده با دادهٔ واقعی بیمار طراحی نشده است.

## قابلیت‌ها

- API مبتنی بر FastAPI برای ثبت، مشاهده، اصلاح فوریت، تأیید و لغو درخواست.
- احراز هویت نمایشی، کلید idempotency و نگه‌داری پایدار درخواست‌ها و رویدادها در SQLite.
- بررسی گروه خونی و فرآورده با ماتریس سازگاری بستهٔ محلی.
- بررسی واجدشرایط‌بودن اهداکننده با قواعد سنی، وزنی، فاصلهٔ اهدای قبلی و تعویق‌های ثبت‌شده.
- انتخاب موجودی سازگار بر مبنای نزدیک‌ترین تاریخ انقضا (FEFO) و پیگیری ETA ارسال.
- اتصال به سرویس‌های محلی Gateway، Travel و Inventory.
- پردازش رویدادهای تکراری یا نامرتب و محاسبهٔ جداگانهٔ اهدای انجام‌شده، تأمین در مسیر و تحویل.
- رابط فارسی RTL برای مشاهدهٔ درخواست‌ها، موجودی، ارسال‌ها و گردش اهداکنندگان.

## فناوری و معماری

Backend با Python، FastAPI و SQLite پیاده‌سازی شده است. قواعد دامنه از فایل‌های بستهٔ داده خوانده می‌شوند و ارتباط با سرویس‌های Gateway، Travel و Inventory از طریق HTTP انجام می‌شود. رابط کاربری با HTML، CSS و JavaScript ساده ساخته شده و همراه Backend ارائه می‌شود؛ مرحلهٔ جداگانهٔ build برای Frontend وجود ندارد.

جزئیات اجزا و جریان ثبت درخواست تا تحویل در [مستند معماری](docs/ARCHITECTURE.md) آمده است.

## ساختار پروژه

| مسیر | کاربرد |
|---|---|
| `app/` | API، تنظیمات، منطق دامنه، ذخیره‌سازی SQLite و اتصال سرویس‌ها |
| `frontend/` | صفحه و فایل‌های رابط کاربری |
| `tests/` | تست‌های گردش کار، اتصال‌ها، مسیریابی و اعتبارسنجی بستهٔ داده |
| `docs/` | مستندات فارسی معماری، API، قواعد داده و راهنمای اجرا |
| `examples/` | نمونهٔ بدنهٔ درخواست و رویدادها |
| `requirements.txt` | وابستگی‌های Python |
| `.env.example` | نمونهٔ تنظیمات محلی |
| `DECLARED.md` | وضعیت قابلیت‌های موجود، آزمایشی و محدودیت‌ها |

### بستهٔ محلی موردنیاز

اجرای دامنه و تست‌های یکپارچه به پوشه‌های `participant_kit/` و `harness/` نیاز دارد. آن‌ها در محیط توسعه باید کنار این README قرار داشته باشند. چون مجوز بازتوزیع عمومی این فایل‌های ارائه‌شده توسط برگزارکننده در دسترس نیست، این دو پوشه و همچنین `starter/` عمداً در Git نادیده گرفته می‌شوند. هنگام دریافت مخزن از GitHub، بستهٔ مجاز را جداگانه از برگزارکننده تهیه کنید. فایل‌های محلی مسابقه در این پروژه حذف نشده‌اند.

## پیش‌نیاز

- Windows 10 یا 11
- Python 3.12
- Node.js فقط برای بررسی نحوی JavaScript؛ برای اجرای برنامه لازم نیست.
- بستهٔ محلی `participant_kit/` و `harness/` برای اجرای کامل و تست‌ها

## نصب در Windows PowerShell

فرمان‌ها را از ریشهٔ مخزن اجرا کنید:

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env.example` فقط مقادیر پیش‌فرض محلی و نمایشی دارد؛ این tokenها عمومی‌اند و نباید برای سرویس واقعی استفاده شوند. ساخت `.env` اختیاری است، چون برنامه همین پیش‌فرض‌های محلی را دارد. برای تغییر مسیر دیتابیس یا نشانی سرویس‌ها، مقادیر را در نشست PowerShell تنظیم کنید یا فایل `.env` محلی را ویرایش کنید. `.env` واقعی و دیتابیس‌ها وارد Git نمی‌شوند.

## اجرای برنامه

اگر برای داشبورد، دادهٔ موجودی زنده می‌خواهید، ابتدا سرویس‌های محلی ارائه‌شده را در سه پنجرهٔ جداگانهٔ PowerShell اجرا کنید:

```powershell
.\.venv312\Scripts\python.exe -m harness.mock_gateway --port 9100 --speed 240
.\.venv312\Scripts\python.exe -m harness.travel_service --port 9200 --speed 240
.\.venv312\Scripts\python.exe -m harness.inventory_service --port 9300 --speed 240 --kit participant_kit
```

در پنجرهٔ چهارم، از ریشهٔ مخزن:

```powershell
$env:DB_PATH = 'run_out\local-demo.db'
$env:SELF_URL = 'http://127.0.0.1:8000'
.\.venv312\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

سپس این نشانی‌ها را باز کنید:

- داشبورد: <http://127.0.0.1:8000/>
- مستندات تعاملی API: <http://127.0.0.1:8000/docs>
- سلامت سرویس: <http://127.0.0.1:8000/health>

سرویس‌های محلی فقط برای توسعه و شبیه‌سازی‌اند. اگر Inventory در دسترس نباشد، snapshot خطای سرویس را گزارش می‌کند و موجودی ساختگی نشان نمی‌دهد.

## مسیرهای API

- `GET /health` و `GET /hospitals`
- `POST /requests`، `GET /requests` و `GET /requests/{request_id}`
- `POST /requests/{request_id}/approve`
- `POST /requests/{request_id}/amend`
- `POST /requests/{request_id}/cancel`
- `POST /callbacks/donor-response`
- `POST /callbacks/donor-progress`
- `POST /callbacks/gateway-status`
- `GET /operations/snapshot?hospital_id=...`

قرارداد کامل درخواست‌ها، callbackها و پاسخ‌ها در [مستند API](docs/API_CONTRACT.md) توضیح داده شده است. مقادیر احراز هویت و محدودهٔ API، پیش‌فرض محلی تمرین‌اند.

## اجرای تست‌ها

با فعال‌بودن بستهٔ محلی و محیط نصب‌شده، از ریشهٔ مخزن اجرا کنید:

```powershell
.\.venv312\Scripts\python.exe -m unittest discover -s tests -v
.\.venv312\Scripts\python.exe -m compileall -q app tests
node --check frontend\static\app.js
.\.venv312\Scripts\python.exe tests\validate_public_kit.py
```

تست قرارداد API به Backend در حال اجرا نیاز دارد:

```powershell
.\.venv312\Scripts\python.exe -m harness.contract_tests --team http://127.0.0.1:8000
```

جزئیات آماده‌سازی سرویس‌ها و اجرای سناریوها در [راهنمای Demo](docs/DEMO.md) است. هر سناریو را با دیتابیس جدا اجرا کنید، چون ساعت شبیه‌سازی در هر اجرا از صفر آغاز می‌شود. Runner سرویس‌های محلی را خودش بالا می‌آورد؛ هم‌زمان mockهای دستی را روی همان پورت‌ها اجرا نکنید.

## پیکربندی

| متغیر | پیش‌فرض | کاربرد |
|---|---|---|
| `PORT` | `8000` | پورت API |
| `SELF_URL` | `http://127.0.0.1:8000` | نشانی callback این سرویس |
| `GATEWAY_URL` | `http://localhost:9100` | ساعت شبیه‌سازی و پیام‌ها |
| `TRAVEL_URL` | `http://localhost:9200` | زمان مسیر |
| `INVENTORY_URL` | `http://localhost:9300` | موجودی و ارسال |
| `HOSPITAL_TOKEN` | `hospital-token` | token نمایشی محلی |
| `COORDINATOR_TOKEN` | `coordinator-token` | token نمایشی هماهنگ‌کننده |
| `ABUSE_RARE_DEMAND_TAIL` | `0.02` | سهم انتهایی تقاضای تاریخی برای بازبینی |
| `ABUSE_BURST_WINDOW_MINUTES` | `5` | پنجرهٔ زمانی بررسی درخواست‌های پیاپی |
| `ABUSE_BURST_REQUESTS` | `5` | تعداد درخواست در پنجره برای بازبینی |
| `KIT_PATH` | `participant_kit` | مسیر بستهٔ دادهٔ محلی |
| `DB_PATH` | `bloodnet.db` | مسیر دیتابیس محلی |

## مستندات و محدودیت‌ها

- [معماری و جریان داده](docs/ARCHITECTURE.md)
- [قرارداد API](docs/API_CONTRACT.md)
- [قواعد داده و تخصیص](docs/DATA_ANALYSIS.md)
- [راهنمای اجرای محلی](docs/DEMO.md)
- [وضعیت قابلیت‌ها](DECLARED.md)

سیاست بازبینی تقاضای غیرمعمول یک قاعدهٔ قابل‌تنظیم و مبتنی بر دادهٔ تاریخی است، نه تشخیص‌گر آموزش‌دیده. برنامه تک‌فرآیندی و محلی است؛ دسترس‌پذیری بالا، احراز هویت تولیدی و استفادهٔ بالینی پیاده‌سازی یا اعتبارسنجی نشده‌اند. برای فهرست کامل قابلیت‌های آزمایشی و پیاده‌سازی‌نشده، [DECLARED.md](DECLARED.md) را ببینید.
#   B l o o d N e t  
 #   B l o o d N e t  
 #   B l o o d N e t  
 