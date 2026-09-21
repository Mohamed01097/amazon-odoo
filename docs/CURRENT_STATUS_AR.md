# الحالة الحالية

**آخر تحديث:** 2026-09-16 17:15 UTC
**إصدار الموديول:** 19.0.10.6.1
**قاعدة البيانات:** amazon_prod12sep
**Amazon Instance:** Amazon Egypt Production (id=6)

## الخصائص المفعّلة في الإنتاج

الخصائص التالية **تعمل فعلياً** في بيئة الإنتاج بتاريخ 2026-09-16:

### Order Pipeline — مفعّل
- Cron الخاص بـ Order Import (id=24) — **متوقف حالياً** (تم تفعيله عند بداية التشغيل ثم أُوقف لاحقاً). هو dispatcher آمن يقرأ فقط ويمكن إعادة تفعيله عند الحاجة.
- Order Import Job Processor (id=25) يعمل كل دقيقة واحدة.
- Order Status Sync (id=26) يعمل كل 15 دقيقة — **وهو الآلية الأساسية الحالية لاكتشاف الطلبات**.
- تم استيراد 43 Amazon Order و 43 Sale Order في Odoo.
- صفر Import Jobs أو Status Sync Jobs فاشلة (11 import job و 23 status sync job إجمالاً).

### FBA Sale Stock Depletion — مفعّل
- FBA Sale Stock Event Processor (id=27) يعمل كل دقيقة واحدة.
- 55 Event تمت معالجتها (جميعها `done`)، صفر `pending`، `failed`، أو `manual_review`.
- التحقق من خصم Sellable: 944 (الافتتاحي) − 42 (المخصوم) = 902 (الحالي). متطابق.

### Cutover V2 — مفعّل
- Cutover Run 3: state=activated. عدد Baselines: 15,269 تغطي 13,321 طلب فريد.
- history_start_at: 2025-09-15 21:02:11 UTC.
- cutover_at: 2026-09-15 21:02:11 UTC.
- يمنع الخصم المزدوج للطلبات التاريخية. تم التحقق من صحته ببيانات إنتاج فعلية.

### FBA Inventory Audits — مفعّل
- Daily Audit Enqueue (id=39) و Audit Processor بكل 5 دقائق (id=40) يعملان.
- آخر Audit: FBAAUDIT/00011 (مكتمل 2026-09-16).
- التطابق المحاسبي مؤكد: Odoo Sellable يطابق المعادلة (944 − sum(D) = 902).

### FBA Inbound Shipments — مفعّل
- Inbound Operation Poller (id=35) يعمل كل دقيقة واحدة.
- Inbound Receiving Sync (id=36) يعمل كل 30 دقيقة.

### المراقبة والعمليات — مفعّلة
- Connection Health Check (id=46) — 15 دقيقة.
- Dashboard Refresh (id=47) — 15 دقيقة.
- Stuck Job Detection (id=48) — 10 دقائق.
- Eligible Retry Dispatch (id=49) — 5 دقائق.
- Operational Alert Evaluation (id=50) — 15 دقيقة.
- Successful Log Cleanup (id=51) — يومياً.
- Process Removal Orders and FBA Events (id=52) — 5 دقائق.
- Reimbursement Matching (id=56) — يومياً.

### Product Mapping — مكتمل
- 19 سجل `amazon.product`، جميعها مربوطة بمنتجات Odoo عن طريق SKU.
- يوجد 2 SKU إضافيين من جهة Amazon (M7-X72T-G8NN و O8-DZJQ-EDXW) يظهران في نتائج Audit كـ "unmapped" — كلاهما بكمية صفر في جميع الفئات.

### FBA Inventory — تم تحميله
- تم تحميل المخزون الافتتاحي من FBAAUDIT/00007 (2026-09-15 21:02:11 UTC).
- القيم الافتتاحية: Sellable=944, Reserved=48, Unsellable=8, Transit=1,571.
- الحالي (2026-09-16 17:15 UTC): Sellable=902, Reserved=48, Unsellable=8, Transit=1,571.
- Stock Moves: 82. Stock Pickings: 33.

### Defense-in-Depth — مفعّل
- `stock_push_interval` = disabled (يمنع Stock Push إلى Amazon عن طريق الخطأ).
- `price_push_interval` = disabled (يمنع Price Push إلى Amazon عن طريق الخطأ).
- `settlement_sync_interval` = disabled (يمنع Settlement Import قبل إعداد المحاسبة).
- `auto_sync_enabled` = False (يمنع تشغيل Master Scheduler).

### ملخص Crons
- **15 مفعّل** من Amazon Crons (راجع `docs/IMPLEMENTER_HANDOFF.md` القسم 9 للقائمة الكاملة).
- **18 متوقف** من Amazon Crons (متوقفة عمداً — خطيرة، أو لم يتم إعدادها بعد، أو غير مطلوبة).

## لم يتم إعدادها بعد — تتطلب عمل Implementer/المحاسب

### Settlement Accounting — في الانتظار
- Settlement Accounting Strategy محددة كـ `settlement_based`، لكن جميع حقول الحسابات/اليوميات الـ 15 فارغة (NULL).
- لا يوجد Settlement Journal، لا Clearing Account، لا Payout Bank Journal.
- صفر Settlements مستوردة، صفر Accounting Entries.
- **محظور بسبب:** المحاسب يحتاج لإعداد Chart of Accounts و Account Mappings.

### إعدادات الضرائب المصرية — في الانتظار
- لا يوجد إعداد ضريبي لـ Amazon Egypt Settlements.
- **محظور بسبب:** قرارات المحاسب ومستشار الضرائب.

### Settlement Cutoff Date — في الانتظار
- غير محدد. يجب تحديده قبل استيراد Settlements.
- **محظور بسبب:** قرار المحاسب.

## متوقفة — تتطلب قرار العميل

### Customer Returns Import — يتطلب قرار
- Cron id=53 متوقف. صفر Return Reports مستوردة.
- **محظور بسبب:** موافقة العميل.

### Inventory Adjustments Import — يتطلب قرار
- Cron id=54 متوقف. صفر Adjustments مستوردة.
- **محظور بسبب:** موافقة العميل وتأكيد السياسة (informational أو stock-moving).

### Reimbursements Import — يتطلب قرار
- Cron id=55 متوقف. صفر Reimbursements مستوردة.
- **محظور بسبب:** موافقة العميل.

### Removal Order Tracking — يتطلب قرار
- Cron id=37 متوقف. صفر Removal Orders.
- **محظور بسبب:** العميل لديه Removals فعلية لتتبعها.

### Product Sync Automation — يتطلب قرار
- Cron id=28 متوقف.
- **محظور بسبب:** قرار العميل بخصوص Product Master (Odoo أو Amazon).

### Price Push — متوقف (حماية)
- Cron id=29 متوقف. Interval محدد كـ `disabled`.
- **محظور بسبب:** قرار العميل بخصوص سياسة الأسعار + موافقة المطور.

### Stock Push — متوقف (حماية)
- Cron id=33 متوقف. Interval محدد كـ `disabled`.
- غير مطلوب لـ FBA. Amazon تدير مخزون مستودعها بنفسها.

### AI Features — غير مهيأة
- لا يوجد AI API Key. جميع AI Intervals بلا تأثير.
- **محظور بسبب:** قرار العميل وتوفير API Key.

## متوقفة بشكل دائم

| العنصر | السبب |
|---|---|
| Master Auto-Sync Scheduler (id=43) | يشغّل جميع أنواع المزامنة بما فيها العمليات الخطيرة. يجب أن يظل متوقفاً. |
| Full Bidirectional Sync (id=42) | يجمع بين القراءة والكتابة. لا يتم تفعيله أبداً لـ FBA. |
| FBM Order Crons (id=32, 34) | النظام يعمل بـ FBA فقط. لا يوجد Merchant Fulfillment. |

## المخاطر المفتوحة

- فروقات Audit بين Odoo و Amazon متوقعة بسبب توقيت Fulfillment و إعادة تصنيف Reserved. الفروقات المستمرة أو المتزايدة تتطلب تحقيقاً.
- يوجد 2 SKU من جهة Amazon بكمية صفر غير مربوطين. يجب المراقبة في حال ظهور كميات.
- إعداد المحاسبة هو المهمة الرئيسية المتبقية قبل التكامل المالي الكامل.
- Price Push و Stock Push يظلان متوقفين ولا يتم تفعيلهما إلا بموافقة صريحة من المطور والعميل.
- سلوك Report/Feed/API قد يتغير؛ يجب مراجعته بشكل دوري ضد توثيق Amazon الرسمي.

## توثيق التسليم

دليل تأهيل Implementer الكامل: [`docs/IMPLEMENTER_HANDOFF.md`](IMPLEMENTER_HANDOFF.md) | [`docs/IMPLEMENTER_HANDOFF_AR.md`](IMPLEMENTER_HANDOFF_AR.md)
