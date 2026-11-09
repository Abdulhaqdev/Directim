# Sotuv agenti — bir nechta Instagram profil uchun AI-sotuvchi

Faqat o'zingiz uchun. Bitta serverda bir nechta Instagram profil ishlaydi: kitoblar, bolalar naborlari, umumiy mahsulotlar va hokazo.

## Qanday ishlaydi

```
Mijoz Direct / komment yozadi
        │
        ▼
Profil aniqlanadi (qaysi Instagram) ──► shu profilning katalogi + biznes matni + xotira
        │
        ▼
AI javob beradi, narxni aytadi, raqam so'raydi ──► raqam olindi ──► 🆕 Telegram guruhga lid
        │
        └─ javobini bilmasa ──► ❓ Telegram'ga savol (#12)
                                  │
                       Siz shu xabarga Reply qilib javob yozasiz
                                  │
                                  ▼
                  ✅ Xotiraga saqlanadi + kutayotgan mijozga darhol yuboriladi
                     Keyingi safar shu savolga agent o'zi javob beradi
```

### Profillar
Har bir Instagram profil — alohida agent: o'z biznes matni (yetkazish, to'lov, uslub), sozlamalari va Telegram guruhi bor.

### Umumiy katalog
Barcha mahsulotlar bitta katalogda turadi, **kategoriya** bilan. Profil qaysi kategoriyalarni sotishini o'zingiz tanlaysiz:

| Profil | Kategoriyalar |
|---|---|
| Kitoblar (@ilmbooks.uz) | Kitoblar, Dekor, Diniy buyumlar |
| Bolalar naborlari | Bolalar, Naborlar |
| Umumiy do'kon | `*` (butun katalog) |

Har bir mahsulotda quyidagilar bo'ladi:
- nomi, narxi, variantlari (rang, o'lcham), sotuvda bor yoki yo'qligi;
- **kalit so'zlar** — mijozlar mahsulotni qanday atashi;
- **javob shabloni** — shu mahsulot so'ralganda agent aynan shu mazmunda javob beradi;
- **bog'langan postlar** — shu post ostidagi komment shu mahsulot haqida deb tushuniladi.

Katalogni to'rt xil yo'l bilan to'ldirish mumkin:
1. **Panelda qo'lda**: «Mahsulotlar» → «+ Mahsulot qo'shish».
2. **Excel/CSV fayl**: «Mahsulotlar» → namuna faylni yuklab oling, to'ldiring va yuklang. Bir xil nomli mahsulot qayta qo'shilmaydi, yangilanadi.
3. **Google Sheets**: jadval havolasini qo'ysangiz, narxlarni jadvalda o'zgartirasiz va agent ularni har 30 daqiqada o'zi yangilaydi.

Jadval ustunlari: `nomi, kategoriya, narx, narx_izoh, tavsif, variantlar, mavjud, kalit_sozlar, shablon, post_idlar`.
4. **Saytdan import**: «Mahsulotlar» → «🌐 Saytdan import» → sayt manzilini kiritasiz (masalan `shoxmarket.uz`) → «Import qilish». Shundan keyin:
   - mahsulotlar nomi, narxi, chegirmasi (eski narxi), tavsifi, rasmi va saytdagi kategoriyasi bilan katalogga tushadi;
   - sayt har 24 soatda qayta tekshiriladi: narx o'zgarsa yangilanadi, mahsulot saytdan olib tashlansa «tugagan» deb belgilanadi;
   - siz panelda o'zgartirgan nom, kategoriya, shablon va tavsif keyingi yangilanishda o'zgarmaydi;
   - bitta mahsulot sahifasining manzilini ham bersa bo'ladi. Shunda faqat o'sha mahsulot olinadi.

   Saytdagi kategoriya nomini profilga ham yozing. Masalan shoxmarket'dagi «Bolalar uchun» va «Sovg'alar» mahsulotlari bolalar profilida chiqishi uchun shu profilning kategoriyalariga `Bolalar uchun, Sovg'alar` deb yozasiz.
   Import qiluvchi istalgan saytda ishlaydi. Avval sahifadagi standart mahsulot ma'lumotini (JSON-LD), keyin meta teglarni va sahifa tuzilishini o'qiydi. Tuzilishi notanish saytda mahsulotni Claude sahifa matnidan o'qib ajratib oladi.

### Xotira
Agent bilmagan savol Telegram guruhga keladi. Siz o'sha xabarga **Reply** qilib javob yozasiz. Shundan keyin:
- javob xotiraga saqlanadi. Savol mahsulotga tegishli bo'lsa, javob shu mahsulotga bog'lanadi;
- shu savolni bergan **barcha kutayotgan mijozlarga** javob darhol yuboriladi (oxirgi 24 soat ichida yozganlarga);
- keyingi safar shu savol kelsa, agent o'zi javob beradi.

Xotirani panelning «Xotira» bo'limida tahrirlash va o'chirish mumkin. Javob faqat bitta profil uchun yoki barcha profillar uchun saqlanadi: masalan «Ish vaqtingiz?» degan savol hamma profilga tegishli bo'ladi.

### Qolgan imkoniyatlar
- **Kommentlar.** Savol yozilgan kommentga agent Direct'da javob yozadi, komment ostida «✅ Direct'ga yozdik» deb qoldiradi. Haqoratni o'chiradi, spamni yashiradi.
- **Siz o'zingiz javob berganda.** Instagram'dan qo'lda yozsangiz, agent o'sha dialogda 12 soat jim turadi. Mijoz operator so'rasa, Telegram'ga «🙋 Operator kerak» xabari boradi.
- **Eslatma.** Raqam qoldirmay jim qolgan mijozga 3 soatdan keyin bitta eslatma yuboriladi.

---

## O'rnatish

### 1. Kalitlar (`.env`)
`.env.example` faylidan nusxa olib, `.env` deb saqlang va qiymatlarni to'ldiring:
- `ANTHROPIC_API_KEY`;
- `TELEGRAM_BOT_TOKEN`;
- `IG_APP_ID`, `IG_APP_SECRET` va `IG_VERIFY_TOKEN`;
- `TELEGRAM_WEBHOOK_SECRET` va `PUBLIC_URL`;
- `ADMIN_PASSWORD`.

### 2. Meta ilovasi (bir marta, ~15 daqiqa — keyin har bir profil bir bosishda ulanadi)
1. developers.facebook.com → **Create app** → turi **Business**.
2. **Instagram** mahsulotini qo'shing → **API setup with Instagram login**.
3. Shu sahifadagi **Instagram app ID** va **Instagram app secret** ni `.env` ga yozing (`IG_APP_ID`, `IG_APP_SECRET`).
4. **Business login settings** → OAuth redirect URI: `https://SERVER/instagram/callback`.
5. **Webhooks** bo'limida:
   - Callback URL: `https://SERVER/webhook`;
   - Verify token: `.env` dagi `IG_VERIFY_TOKEN`;
   - `messages` va `comments` maydonlariga obuna bo'ling.
6. **App roles → Roles → Instagram testers** bo'limiga Instagram akkauntlaringizni qo'shing. Keyin har bir akkauntda taklifni qabul qiling: Instagram → Sozlamalar → Ilovalar va saytlar → Tester takliflari.
   Ilovani **Live** rejimga o'tkazsangiz (buning uchun Privacy Policy havolasi kerak), bu qadam shart emas.

### 3. Profilni ulash — Morechat'dagidek, bir bosishda
1. Chrome'da kerakli Instagram akkauntga kirib turing.
2. Panelda: «Profillar» → **+ Instagram bilan ulash**. Kitoblar profili uchun esa uning sahifasidagi **Instagram bilan ulash** tugmasini bosing.
3. Instagram oynasida **Ruxsat berish** ni bosing. Shundan keyin profil o'zi yaratiladi, webhook yoqiladi va postlar yuklanadi. Token har 7 kunda avtomatik yangilanadi.
4. Profil sahifasida kategoriyalarni (masalan «Bolalar, Naborlar») va biznes matnini to'ldiring.
5. Instagram ilovasida: Sozlamalar → Xabarlar → **Ulangan vositalar** → xabarlarga ruxsat bering.

Boshqa akkauntni ulash uchun Chrome'da Instagram'dan chiqib, o'sha akkauntga kiring va yana tugmani bosing.

### 4. Telegram
1. Botni lidlar guruhiga qo'shing.
2. `python -m app.setup_tg` buyrug'ini ishga tushiring: u guruh ID'sini ko'rsatadi.
3. Har bir profil uchun alohida guruh ishlatsangiz, uning ID'sini profil sahifasiga yozing.
4. Panelning bosh sahifasidagi **Telegram'ga test xabar** tugmasini bosing. Shunda Telegram'da Reply orqali javob berish ham yoqiladi.

### 5. Server
Instagram va Telegram xabar yuborishi uchun server internetdan https orqali ochiq bo'lishi kerak.
- **Railway / Render**: Dockerfile o'zi topiladi. `.env` dagi qiymatlarni Variables bo'limiga kiriting va `data/` papkasiga doimiy disk ulang.
- **VPS**: `uvicorn app.main:app --host 0.0.0.0 --port 8000`, oldiga nginx va SSL qo'ying.

### 6. Ishga tushirish tartibi
1. **Test rejimi.** Profil sozlamalarida `test_rejimi.yoqilgan: true` qiling va o'zingizning ikkinchi akkauntingizni ro'yxatga yozing. Shu akkauntdan yozib sinab ko'ring.
2. **Morechat'ni o'chiring.** Aks holda mijozga ikki tizim birdan javob yozadi.
3. Test rejimini o'chiring (`false`).

## Sinov
```bash
python -m pytest -q
```
