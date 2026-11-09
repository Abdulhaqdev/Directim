"""Claude bilan ishlash: Direct javoblari, komment tasnifi, eslatma matni."""
from __future__ import annotations

import logging
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from .settings import Config

log = logging.getLogger("ai")


class DMResult(BaseModel):
    reply: str = Field(description="Mijozga yuboriladigan javob matni. Bo'sh bo'lsa hech narsa yuborilmaydi.")
    phone: Optional[str] = Field(description="Mijoz yozgan telefon raqam, yoki mijoz avvalgi raqamini tasdiqlasa o'sha raqam; aks holda null")
    name: Optional[str] = Field(description="Mijoz o'z ismini aytgan bo'lsa")
    interest: Optional[str] = Field(description="Mijoz qiziqayotgan mahsulot(lar) nomi")
    product_id: Optional[int] = Field(description="Katalogdagi mahsulot raqami [#N], agar aniq bo'lsa")
    for_whom: Optional[str] = Field(description="Kim uchun xarid qilyapti (aytgan bo'lsa)")
    region: Optional[str] = Field(description="Viloyat / shahar (aytgan bo'lsa)")
    needs_human: bool = Field(description="Operator aralashuvi kerakmi")
    handoff_reason: Optional[str] = Field(description="needs_human=true bo'lsa qisqa sabab")
    unknown_question: Optional[str] = Field(
        description="Bilim bazasida javobi yo'q savol (bo'lsa) — egasi keyin javob yozadi"
    )
    summary: str = Field(description="Operator uchun suhbatning 1 gaplik xulosasi")
    temperature: Literal["issiq", "iliq", "sovuq"] = Field(
        default="iliq", description="Xarid ehtimoli: issiq — aniq olmoqchi/raqam berdi, iliq — qiziqyapti, sovuq — shunchaki so'rayapti")
    lead_reason: str = Field(default="", description="Nega shunday baho: qisqa, masalan 'aniq xarid-niyati · raqam berdi'")


class ProductExtract(BaseModel):
    is_product: bool = Field(description="Bu sahifa bitta mahsulot sahifasimi")
    name: Optional[str] = Field(description="Mahsulot nomi")
    price: Optional[int] = Field(description="Joriy narx so'mda (faqat raqam)")
    old_price: Optional[int] = Field(description="Chegirmadan oldingi narx so'mda, bo'lsa")
    category: Optional[str] = Field(description="Saytdagi kategoriya")
    description: Optional[str] = Field(description="Qisqa tavsif, 1-3 gap, sahifadagi ma'lumot asosida")


class HistoryFAQ(BaseModel):
    question: str = Field(description="Mijozlar beradigan savol (umumiy, qisqa shaklda)")
    answer: str = Field(description="Do'kon tomoni bergan aniq javob (faqat yozishmada bor fakt)")
    product: Optional[str] = Field(description="Qaysi mahsulot haqida (bo'lsa)")
    times: int = Field(description="Taxminan necha marta so'ralgan")


class HistoryAnalysis(BaseModel):
    faqs: list[HistoryFAQ] = Field(description="Do'kon aniq javob bergan, takrorlanadigan savollar")
    open_questions: list[str] = Field(description="Mijozlar so'ragan, lekin do'kon aniq javob bermagan savollar")
    tips: list[str] = Field(description="Sotuvni yaxshilash bo'yicha 3-6 ta qisqa kuzatish (qaysi ibora raqam olishga yordam bergan, qayerda mijoz yo'qolgan, shikoyatlar)")


CommentCategory = Literal["SAVOL", "XARID", "NEYTRAL", "SHIKOYAT", "HAQORAT", "SALBIY", "SPAM", "BOSHQA"]


class CommentResult(BaseModel):
    category: CommentCategory
    public_reply: str = Field(description="Komment ostiga ochiq javob. Kerak bo'lmasa bo'sh.")
    dm_message: str = Field(description="Direct'ga yoziladigan birinchi xabar. Kerak bo'lmasa bo'sh.")
    public_fallback: str = Field(default="", description="Direct'ga yozib bo'lmasa komment ostiga yoziladigan to'liq qisqa javob")
    unknown_question: Optional[str] = Field(description="Bilim bazasida javobi yo'q savol (bo'lsa)")
    product_id: Optional[int] = Field(description="Katalogdagi mahsulot raqami [#N], agar aniq bo'lsa")
    reason: str = Field(description="Tasnif sababi, 1 gap")


def _price(p: dict) -> str:
    if not p.get("price"):
        return "narxi ko'rsatilmagan (operator aytadi)"
    text = f"{p['price']:,}".replace(",", " ") + " so'm"
    return text + (f" ({p['price_note']})" if p.get("price_note") else "")


def build_knowledge(business_text: str, products: list[dict], memory: list[dict], posts: list[dict]) -> str:
    """Profil uchun bilim bazasi: biznes matni + katalog + xotira + postlar."""
    parts = ["# BILIM BAZASI (faqat shu ma'lumotlarga tayanasan)", "", "## Biznes haqida", business_text.strip() or "(yozilmagan)"]
    if products:
        parts += ["", "## Mahsulotlar katalogi (narxlar shu yerdan olinadi)"]
        for p in products:
            line = f"- [#{p['id']}] «{p['name']}» ({p.get('category') or 'Umumiy'}) — {_price(p)}"
            if p.get("old_price") and p.get("price") and p["old_price"] > p["price"]:
                line += f" — CHEGIRMADA (avvalgi narxi {p['old_price']:,} so'm)".replace(",", " ")
            line += "" if p.get("in_stock", 1) else " — HOZIR MAVJUD EMAS"
            if p.get("variants"):
                line += f"\n  Variantlar: {p['variants']}"
            if p.get("description"):
                line += f"\n  Tavsif: {p['description']}"
            if p.get("keywords"):
                line += f"\n  Mijozlar shunday atashi mumkin: {p['keywords']}"
            if p.get("template"):
                line += f"\n  JAVOB SHABLONI (shu mahsulot so'ralganda shu mazmunda javob ber): {p['template']}"
            parts.append(line)
    if memory:
        parts += ["", "## Xotira — egasi o'zi bergan javoblar (aniq shu javoblardan foydalan)"]
        for m in memory:
            about = f" [«{m['product_name']}» haqida]" if m.get("product_name") else ""
            parts.append(f"- S{about}: {m['question']}\n  J: {m['answer']}")
    if posts:
        parts += ["", "## Instagram postlari (qo'shimcha ma'lumot; narx katalogda yoki post matnida yozilgan bo'lsagina aytiladi)"]
        for post in posts:
            caption = (post.get("caption") or "").strip().replace("\n", " ")
            if caption:
                parts.append(f"- [post {post.get('id')}] {caption[:600]}")
    return "\n".join(parts)


def dm_rules(cfg: Config, our_username: str) -> str:
    ism = cfg.get("agent.ism", "do'kon menejeri")
    jins = "ayol" if cfg.get("agent.jins", "ayol") == "ayol" else "erkak"
    uslub = cfg.get("agent.uslub", "")
    lead_qs = cfg.get("sotuv.lid_savollari", []) or []
    lead_qs_txt = "\n".join(f"  - {q}" for q in lead_qs) or "  - (yo'q)"
    narx = "Narx so'ralsa, bilim bazasidagi aniq narxni darhol ayt." if cfg.get("sotuv.narx_aytish", True) else (
        "Narxni yozma — operator aytadi, raqam so'ra."
    )
    return f"""# VAZIFA: Instagram Direct sotuvchisi
Sen — {ism}, Instagram'dagi @{our_username or 'dokon'} sahifasining sotuv menejerisan ({jins}). Mijozlar bilan Direct'da yozishasan.

## Asosiy maqsad
1) Mijoz qaysi mahsulotga qiziqayotganini aniqla. 2) Shu mahsulot narxi va shartlarini qisqa ayt. 3) Telefon raqamini ol — operatorlar bog'lanib buyurtmani rasmiylashtiradi.

## Uslub
{uslub}
- Mijoz qaysi tilda va alifboda yozsa (o'zbek lotin, o'zbek kirill, rus) — aynan shunda javob ber.
- Qisqa yoz: odatda 1–3 gap, chat uslubida. Emoji kam (0–1 ta). Ro'yxat va sarlavha ishlatma.
- Birinchi xabarda salomlash ("Assalomu alaykum!"), keyingilarida qayta salomlashma.
- Xushmuomala, ishonchli, bosim o'tkazmasdan.

## Qat'iy qoidalar
- Faqat BILIM BAZASIdagi faktlarni ayt. Xotira bo'limidagi javoblar egasi tomonidan tasdiqlangan — ular eng ishonchli manba. Mahsulotda JAVOB SHABLONI bo'lsa, shu mazmunda (mijoz tiliga moslab) javob ber.
- Katalogda yo'q yoki "HOZIR MAVJUD EMAS" deb belgilangan mahsulotni mavjud dema. Narx, aksiya, chegirma, kafolat, muddat, o'lcham, sahifa soni, material, komplektni o'zingdan to'qima.
- {narx}
- Javobni bilmasang: "Bu savolni operatorimizga yo'naltiraman" mazmunida yoz, raqam so'ra va savolni `unknown_question` ga yoz.
- Mijozni hech qachon saytga yo'naltirma. Buyurtma — telefon raqam orqali.
- Raqam so'rash iborasi: "Raqamingizni qoldiring, operatorlarimiz tez orada bog'lanadi." (mijoz tilida). Mijoz narx, buyurtma yoki yetkazishni so'rasa — raqamni shu javobdayoq so'ra. Har xabarda takrorlama.
- Raqam olinganidan keyin uni qayta so'rama. Tasdiq matnini ("Raqamingiz qabul qilindi") o'zing yozma — tizim o'zi qo'shadi. Rahmat ayt va kerak bo'lsa qolgan savolni ber.
- Qo'shimcha lid savollari (bittadan, tabiiy, so'roqqa aylantirmasdan; raqam olingach yoki suhbat mos kelganda):
{lead_qs_txt}
- Mijoz operator/odam bilan gaplashmoqchi bo'lsa, shikoyat, qaytarish, to'lov muammosi, noto'g'ri buyurtma, aniq yetkazish sanasi yoki boshqa murakkab holat bo'lsa: `needs_human=true`, mijozga operator tez orada bog'lanishini ayt (raqam bo'lmasa so'ra).
- "Sen botmisan?" deb so'rashsa yolg'on gapirma: virtual yordamchi ekaningni ayt, operator ham bog'lanishi mumkinligini qo'sh.
- Haqoratga haqorat bilan javob berma; xotirjam va qisqa bo'l.
- Mijozning ismi ma'lum bo'lsa (dialog holatida), birinchi javobda ismi bilan murojaat qil: "Assalomu alaykum, Feruza!".
- E'tirozlar: "qimmat ekan", "men arzonroq olganman" desa — tushunishingni ayt, mahsulot qiymatini qisqa eslat va asosiy afzallikni ayt: oldindan to'lov yo'q, kelganda ko'rib-tekshirib to'laydi, rasmdagidek bo'lmasa rad etishi mumkin. O'zingdan chegirma va'da qilma.
- Mijoz noto'g'ri mahsulot, boshqa nashr yoki eski muqova kelganidan, aldanganidan shikoyat qilsa: uzr so'ra, bahslashma, `needs_human=true` va operator tez orada bog'lanishini ayt.
- "Yangi nashr", "original", "sifat kafolati" kabi so'zlarni faqat bilim bazasida yozilgan bo'lsagina ishlat.
- Mijoz faqat post yoki reels ulashsa (matnsiz), qaysi mahsulot qiziqtirayotganini aniqlashtir; kontekstdan aniq bo'lsa — darhol narxini ayt.
- Mijoz story yoki postga javob yozgan bo'lsa, kontekstdagi post matnidan mahsulotni aniqla.
- Mijoz rasm yuborsa, rasmda nima borligini tushunib, bilim bazasidagi mahsulotga mos kelsa — shu haqida javob ber; mos kelmasa — aniqlashtir.
- Mijoz ovozli xabar yuborsa, uni eshita olmaysan — muloyimlik bilan matn bilan yozishni so'ra.

## Chiqish maydonlari
`reply` — mijozga yuboriladigan matn. `phone` — mijoz yozgan raqam (faqat mijozning o'z xabaridan). `interest`, `for_whom`, `region`, `name` — suhbatdan ma'lum bo'lsa. `summary` — operator uchun 1 gap. `product_id` — mijoz so'rayotgan mahsulotning katalogdagi [#N] raqami (aniq bo'lsa).
- `unknown_question` ni qisqa va umumiy shaklda yoz (masalan "«Saodat asri» sahifa soni"), shunda bir xil savollar bitta bo'lib yig'iladi.
"""


def comment_rules(cfg: Config, our_username: str) -> str:
    cta = cfg.get("komment.cta_matni", "✅ Direct'ga yozdik")
    return f"""# VAZIFA: Instagram kommentlari
Sen @{our_username or 'dokon'} sahifasining menejerisan. Post ostidagi kommentni tasnifla va javob tayyorla.

## Toifalar
- SAVOL — mahsulot, narx, yetkazish, mavjudlik haqida savol ("narxi qancha", "nech pul", "dastavka bormi", "+", "info").
- XARID — sotib olish istagi ("olmoqchiman", "menga ham", "buyurtma").
- NEYTRAL — maqtov, duo, emoji, "Mashallah", "zo'r", minnatdorchilik.
- SHIKOYAT — haqiqiy mijoz muammosi bizning do'kon bilan (buyurtma kelmadi, boshqa nashr/muqova keldi, sifat, kechikish, telefon ko'tarilmadi).
- HAQORAT — so'kinish, haqorat, tuhmat, odobsiz so'zlar.
- SALBIY — ayblov, sahifa/mahsulotni yomonlash, salbiy fikr (haqiqiy buyurtma muammosi bo'lmasa).
- SPAM — reklama, begona link, "obuna bo'ling", bot kommentlar.
- BOSHQA — foydalanuvchilar o'zaro gaplashishi, do'stini belgilash (@ism), mavzuga aloqasiz gap.
  Diniy yoki hayotiy fikr-mulohaza, nasihat, boshqa post haqida yozilgan gap — SHIKOYAT ham, SALBIY ham emas: NEYTRAL yoki BOSHQA.
- "Qimmat ekan", "men arzonroq olganman" — SALBIY emas, SAVOL (narx e'tirozi): DM'da e'tirozga muloyim javob ber.

## Javoblar
- `public_reply` (komment ostida ochiq): juda qisqa, komment tilida va alifbosida.
  - SAVOL/XARID: bir qisqa foydali jumla (masalan mahsulot nimaligi) + oxirida "{cta}" (komment alifbosiga moslab). Ochiq javobda telefon raqam so'rama.
  - NEYTRAL: qisqa iliq minnatdorchilik (masalan "Rahmat! 🤍", duo bo'lsa "Amin, rahmat!"). Faqat emoji bo'lsa — 1 ta emoji yoki "Rahmat 🤍".
  - SHIKOYAT: "Noqulaylik uchun uzr. Direct'ga yozdik — masalani o'sha yerda hal qilamiz." mazmunida.
  - HAQORAT, SALBIY, SPAM, BOSHQA: bo'sh qoldir.
- `public_fallback`: SAVOL/XARID uchun — agar Direct'ga yozib bo'lmasa (mijozda Direct yopiq), komment ostiga yoziladigan to'liq qisqa javob (narx va asosiy shart bilan, telefon raqam so'ramasdan). Boshqa toifalarda bo'sh.
- `dm_message` (Direct'ga birinchi xabar): faqat SAVOL, XARID va SHIKOYAT uchun. Salomlash + savolga BILIM BAZASI asosida aniq javob (narx ma'lum bo'lsa ayt) + keyingi qadam (nima kerakligini so'rash yoki raqam so'rash). 1–3 gap. Boshqa toifalarda bo'sh.
- Post katalogdagi mahsulotga bog'langan bo'lsa, `product_id` ga uning raqamini yoz.
- Bilim bazasida javob bo'lmasa — to'qima, `unknown_question` ga yoz va DM'da operator aniqlashtirishini ayt.
"""


def followup_rules() -> str:
    return """# VAZIFA: eslatma
Mijoz bir necha soatdan beri javob bermayapti va telefon raqamini qoldirmagan. Suhbat mazmuniga mos bitta qisqa, bosimsiz eslatma yoz (1–2 gap, mijoz tilida va alifbosida): qiziqqan mahsulotini eslat va raqam qoldirsa operator bog'lanishini ayt. Qayta salomlashma. `reply` maydoniga yoz, qolgan maydonlarni suhbatdan to'ldir."""


class AI:
    def __init__(self, api_key: str, model: str, effort: str = "low"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key or None)
        self.model = model
        self.effort = effort
        self.last_error = ""

    def _system(self, knowledge: str, task: str) -> list[dict]:
        # Bilim bazasi birinchi — Direct va komment so'rovlarida bir xil prefiks keshlanadi.
        return [
            {"type": "text", "text": knowledge, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": task, "cache_control": {"type": "ephemeral"}},
        ]

    async def _parse(self, system: list[dict], messages: list[dict], schema: type[BaseModel]):
        kwargs = dict(
            model=self.model,
            max_tokens=4000,
            system=system,
            messages=messages,
            output_format=schema,
            output_config={"effort": self.effort},
        )
        # Opus 5.5 / Opus 5 / Fable / Sonnet 5.5: xavfsizlik klassifikatori rad etsa server tomonda boshqa modelda qayta urinadi.
        if self.model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            response = await self.client.beta.messages.parse(**kwargs)
        except anthropic.RateLimitError:
            self.last_error = "Claude so'rovlar limiti (429)"
            log.warning(self.last_error)
            return None
        except anthropic.AuthenticationError:
            self.last_error = "ANTHROPIC_API_KEY noto'g'ri"
            log.error(self.last_error)
            return None
        except anthropic.APIStatusError as e:
            self.last_error = f"Claude API xato {e.status_code}: {e.message[:200]}"
            log.error(self.last_error)
            return None
        except anthropic.APIConnectionError:
            self.last_error = "Claude bilan aloqa yo'q (tarmoq)"
            log.error(self.last_error)
            return None
        except Exception as e:  # noqa: BLE001 — masalan, javob sxemaga mos kelmadi
            self.last_error = f"Claude javobini o'qib bo'lmadi: {e}"[:300]
            log.exception("Claude javobini qayta ishlashda xato")
            return None
        if response.stop_reason == "refusal":
            self.last_error = "Claude so'rovni rad etdi"
            log.warning("Claude so'rovni rad etdi: %s", response.stop_details)
            return None
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            self.last_error = "Claude javobi chala qoldi"
            return None
        self.last_error = ""
        return response.parsed_output

    async def analyze_history(self, knowledge: str, transcripts: str) -> HistoryAnalysis | None:
        task = ("# VAZIFA: eski yozishmalarni tahlil qilish\n"
                "Quyida do'konning Instagram Direct'dagi eski yozishmalari (M — mijoz, D — do'kon). Telefon raqamlar yashirilgan.\n"
                "1) Do'kon ANIQ javob bergan va takrorlanadigan savollarni `faqs` ga yoz — javob faqat do'kon yozgan faktdan "
                "olinsin, o'zingdan qo'shma. Bilim bazasida allaqachon to'liq bor bo'lgan savollarni qo'shma.\n"
                "2) Do'kon javob bermagan yoki 'aniqlab beramiz' degan savollarni `open_questions` ga yoz.\n"
                "3) `tips` — sotuv bo'yicha kuzatishlar. Hammasi o'zbek tilida, qisqa.")
        return await self._parse(self._system(knowledge, task), [{"role": "user", "content": transcripts}], HistoryAnalysis)

    async def extract_product(self, text: str, url: str) -> ProductExtract | None:
        system = [{"type": "text", "text": (
            "Sen onlayn do'kon sahifasidan mahsulot ma'lumotini ajratasan. Faqat sahifada yozilgan narsani yoz, "
            "taxmin qilma. Narxlarni so'mda butun son qilib yoz (\"149,000 so'm\" → 149000).")}]
        return await self._parse(system, [{"role": "user", "content": f"URL: {url}\n\nSahifa matni:\n{text}"}], ProductExtract)

    async def dm_reply(self, knowledge: str, rules: str, messages: list[dict]) -> DMResult | None:
        return await self._parse(self._system(knowledge, rules), messages, DMResult)

    async def classify_comment(self, knowledge: str, rules: str, comment_block: str) -> CommentResult | None:
        return await self._parse(
            self._system(knowledge, rules), [{"role": "user", "content": comment_block}], CommentResult
        )
