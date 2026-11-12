# راهنمای اجرای محلی

این راهنما برای اجرای داشبورد و سناریوهای محلی است. پیش از اجرا، وابستگی‌ها را طبق بخش نصب README آماده کنید و بستهٔ مجاز `harness/` و `participant_kit/` را کنار ریشهٔ مخزن قرار دهید.

## نمایش داشبورد با دادهٔ موجودی زنده

ابتدا در سه پنجرهٔ جداگانهٔ PowerShell از ریشهٔ پروژه، سرویس‌های محلی را اجرا کنید:

```powershell
.\.venv312\Scripts\python.exe -m harness.mock_gateway --port 9100 --speed 240
```

```powershell
.\.venv312\Scripts\python.exe -m harness.travel_service --port 9200 --speed 240
```

```powershell
.\.venv312\Scripts\python.exe -m harness.inventory_service --port 9300 --speed 240 --kit participant_kit
```

در پنجرهٔ چهارم Backend را با دیتابیس جداگانه اجرا کنید:

```powershell
$env:DB_PATH = 'run_out\dashboard-demo.db'
$env:SELF_URL = 'http://127.0.0.1:8000'
.\.venv312\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

داشبورد در <http://127.0.0.1:8000/> و مستندات API در <http://127.0.0.1:8000/docs> در دسترس‌اند. اگر سرویس Inventory خاموش باشد، داشبورد خطا را نشان می‌دهد و موجودی جعلی جای آن نمی‌گذارد.

## اجرای تست قرارداد

با Backend در حال اجرا، در یک پنجرهٔ دیگر اجرا کنید:

```powershell
.\.venv312\Scripts\python.exe -m harness.contract_tests --team http://127.0.0.1:8000
```

## اجرای سناریو

Runner سرویس‌های خودش را اجرا می‌کند. سرویس‌های در حال اجرا را متوقف نکنید؛ برای جلوگیری از تداخل، در مثال زیر از درگاه‌های جایگزین استفاده شده است. Backend را با دیتابیس تازه و نشانی همین درگاه‌ها بالا بیاورید:

```powershell
$env:GATEWAY_URL = 'http://127.0.0.1:19100'
$env:TRAVEL_URL = 'http://127.0.0.1:19200'
$env:INVENTORY_URL = 'http://127.0.0.1:19300'
$env:DB_PATH = 'run_out\P-normal.db'
$env:SELF_URL = 'http://127.0.0.1:8022'
.\.venv312\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8022
```

سپس در پنجرهٔ دوم:

```powershell
.\.venv312\Scripts\python.exe -m harness.runner --team http://127.0.0.1:8022 --scenario P-normal --speed 480 --gateway-port 19100 --travel-port 19200 --inventory-port 19300 --out run_out\P-normal
```

برای سناریوهای دیگر، یکی از مقادیر `P-amend` یا `P-abuse` را به‌عنوان `--scenario` و نامی تازه برای دیتابیس و مسیر `--out` انتخاب کنید. برای هر سناریو، Backend را جداگانه با دیتابیس تازه راه‌اندازی کنید، چون ساعت شبیه‌سازی از صفر آغاز می‌شود. مقادیر `--gateway-port`، `--travel-port` و `--inventory-port` باید با نشانی‌های تنظیم‌شده برای Backend یکسان و آزاد باشند.

## نکتهٔ تحویل و پاک‌سازی

پوشهٔ `run_out/` برای دیتابیس و گزارش اجرای محلی است و از Git مستثنا شده. دیتابیس فعال یا قبلی را برای اجرای Demo پاک یا جایگزین نکنید.

تمام مقادیر، رویدادها و رفتار سرویس‌های این Demo ساختگی و مخصوص شبیه‌سازی‌اند.
