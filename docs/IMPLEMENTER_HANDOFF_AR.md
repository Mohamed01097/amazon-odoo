# Amazon Egypt FBA Connector — دليل تأهيل Implementer والتسليم

**الموديول:** `sdlc_amazon_connector`
**إصدار Odoo:** 19 Community
**قاعدة البيانات:** `amazon_prod12sep`
**Amazon Instance:** Amazon Egypt Production (id=6)
**Marketplace:** Amazon Egypt (`ARBP9OOSHTCHU`)
**Seller ID:** `A2LMV58FMUN2ZD`
**تاريخ التسليم:** 2026-09-16 UTC
**نطاق الوثيقة:** دليل تأهيل شامل، دليل وظيفي، دليل تشغيلي، تسليم المحاسبة، ودليل السلامة

---

## جدول المحتويات

1. [نظرة عامة تنفيذية](#1-نظرة-عامة-تنفيذية)
2. [جولة في الموديول لأول مرة](#2-جولة-في-الموديول)
3. [إعداد Amazon Instance](#3-إعداد-amazon-instance)
4. [ربط المنتجات و SKU](#4-ربط-المنتجات-و-sku)
5. [بنية مخزون FBA](#5-بنية-مخزون-fba)
6. [المخزون الافتتاحي / تاريخ التشغيل](#6-المخزون-الافتتاحي)
7. [Cutover V2 — شرح للمستخدمين الوظيفيين](#7-cutover-v2)
8. [سير عمل استيراد الطلبات](#8-سير-عمل-استيراد-الطلبات)
9. [الأتمتة المشغّلة حالياً](#9-الأتمتة-المشغّلة-حالياً)
10. [قواعد السلامة الحرجة](#10-قواعد-السلامة-الحرجة)
11. [حالة الإنتاج الحالية](#11-حالة-الإنتاج-الحالية)
12. [المراقبة والعمليات اليومية](#12-المراقبة-والعمليات-اليومية)
13. [دليل استكشاف الأخطاء](#13-دليل-استكشاف-الأخطاء)
14. [تسليم المحاسبة و Settlement](#14-تسليم-المحاسبة-و-settlement)
15. [إجراء اختبار أول Settlement](#15-إجراء-اختبار-أول-settlement)
16. [Returns / Removals / Reimbursements](#16-returns-removals-reimbursements)
17. [شحنات FBA الواردة](#17-شحنات-fba-الواردة)
18. [قرارات سياسة المنتج / السعر](#18-قرارات-سياسة-المنتج-السعر)
19. [مصفوفة المسؤوليات](#19-مصفوفة-المسؤوليات)
20. [قائمة الممنوعات](#20-قائمة-الممنوعات)
21. [قائمة مهام اليوم الأول](#21-قائمة-مهام-اليوم-الأول)
22. [مصفوفة حالة التشغيل](#22-مصفوفة-حالة-التشغيل)
23. [ملخص التسليم النهائي](#23-ملخص-التسليم-النهائي)
24. [ابدأ من هنا — Implementer](#implementer-ابدأ-من-هنا)
25. [قائمة تحقق قبول التسليم](#قائمة-تحقق-قبول-التسليم)

---

## 1. نظرة عامة تنفيذية

### ماذا يفعل هذا التكامل

هذا الموديول المخصص يربط Amazon Egypt Seller Central بـ Odoo 19 Community. يتيح للشركة إدارة مبيعات Amazon والمخزون والبيانات المالية داخل Odoo دون الحاجة للتبديل بين الأنظمة.

العميل يبيع منتجات العناية بالسيارات (علامة IONIC التجارية) على Amazon Egypt باستخدام **Fulfillment by Amazon (FBA)**. هذا يعني:

- المنتجات مخزّنة في مستودع Amazon في مصر.
- Amazon تقوم بالتجهيز والتعبئة والشحن للعملاء.
- Amazon تحصّل المدفوعات من العملاء.
- Amazon تقوم دورياً بتسوية المدفوعات إلى حساب البائع البنكي، بعد خصم العمولات والرسوم.

دور Odoo هو:

- معرفة الطلبات التي استلمتها Amazon ونفّذتها.
- تتبع مستويات مخزون FBA (كم وحدة تحتفظ بها Amazon).
- تسجيل الأثر المالي للمبيعات والرسوم والمرتجعات والمدفوعات.
- توفير مصدر واحد موثوق لتقارير الأعمال.

### كيف يتواصل Amazon و Odoo

```
Amazon Seller Central
        ↕
Amazon SP-API (Selling Partner API)
        ↕
sdlc_amazon_connector (هذا الموديول في Odoo)
        ↕
Odoo Database (طلبات، منتجات، مخزون، محاسبة)
```

يستخدم الموديول Amazon SP-API لـ **قراءة** بيانات الطلبات ومستويات المخزون و Settlements و Returns وغيرها من الأدلة. ويمكنه أيضاً **الكتابة** إلى Amazon (دفع الأسعار، دفع مستويات المخزون)، لكن خصائص الكتابة **متوقفة عمداً** في هذا التشغيل الخاص بـ FBA.

### تدفقات البيانات الرئيسية

#### Orders

يستلم Amazon طلب عميل ← يستورده الموديول إلى Odoo ← يتم إنشاء Sale Order في Odoo ← عندما ينفّذ Amazon الطلب، يسجل Odoo التنفيذ ويخصم من مخزون FBA.

#### Inventory

يحتفظ Amazon بالمخزون الفعلي في مستودعه ← يقرأ الموديول تقرير مخزون Amazon يومياً ← يحتفظ Odoo بسجلات مخزون FBA الخاصة به التي يتم خصمها عند تنفيذ الطلبات ← تقارن عمليات Audit اليومية سجلات Odoo مع أرقام Amazon الفعلية.

#### Products

لدى Amazon قوائم منتجات محددة بـ SKU ← كل Amazon SKU مربوط بمنتج Odoo ← هذا الربط ضروري لعمل الطلبات والمخزون بشكل صحيح.

#### Prices

يمكن سحب الأسعار من Amazon (قراءة) أو دفعها إلى Amazon (كتابة). حالياً، **Price Push متوقف**. قرارات الأسعار تحتاج موافقة العميل.

#### Settlements

يرسل Amazon تقارير Settlement كل أسبوعين تبيّن: الإيرادات المحصّلة، الرسوم المخصومة، المرتجعات، التعويضات، وصافي المبلغ المدفوع. يمكن للموديول استيراد هذه التقارير وإنشاء قيود محاسبية مسودة. **هذا لم يتم إعداده بعد** — يحتاج المحاسب لإعداد اليوميات وربط الحسابات أولاً.

#### Payments / Payouts

يودع Amazon صافي مبلغ Settlement في بنك البائع. يمكن للموديول تسجيل دليل الدفع والمساعدة في مطابقة Amazon Clearing Account مع المعاملات البنكية. **لم يتم إعداده بعد.**

#### Returns

عندما يُرجع عميل منتجاً إلى مستودع Amazon FBA، يمكن للموديول استيراد دليل المرتجع. المرتجعات **لا** تزيد مخزون Odoo تلقائياً — هي سجلات معلوماتية فقط. **لم يتم تفعيلها بعد.**

#### Removals

يمكن للبائع طلب من Amazon إعادة شحن مخزون FBA أو التخلص منه. يتتبع الموديول Removal Orders ودليل الشحن. **لم يتم تفعيلها بعد.**

#### Reimbursements

قد تعوّض Amazon البائع عن مخزون FBA المفقود أو التالف. يستورد الموديول سجلات التعويض كدليل مالي. **لم يتم تفعيلها بعد.**

#### Inbound FBA Shipments

عندما يحتاج البائع لإرسال مخزون جديد إلى مستودع Amazon FBA، يدير الموديول سير عمل الوارد بالكامل: التعبئة، التوزيع، الملصقات، التتبع، الإرسال، والاستلام. سير عمل Inbound **مفعّل** ويمكن استخدامه من خلال واجهة Odoo.

---

## 2. جولة في الموديول

بعد تسجيل الدخول إلى Odoo، ابحث عن قائمة **Amazon** الرئيسية في شريط التنقل العلوي. جميع وظائف الموديول تقع تحت هذه القائمة.

### هيكل القائمة

#### Amazon > Dashboard

- **الغرض:** نظرة عامة على حالة التكامل مع Amazon.
- **ما تراه:** بطاقات ملخص، حالة الاتصال، النشاط الأخير.
- **الإجراءات:** عرض فقط. لا توجد عمليات خطيرة.
- **متى تستخدم:** يومياً — للحصول على فحص سريع للصحة.

#### Amazon > Configuration

| القائمة الفرعية | الغرض | الإجراءات |
|---|---|---|
| **Instances** | إعدادات اتصال حساب Amazon | عرض/تعديل إعدادات Instance |
| **Settings** | إعدادات على مستوى الموديول | لمسؤول النظام فقط |
| **User Guide** | توثيق مدمج | قراءة فقط |
| **AI Features Overview** | وصف أدوات AI | قراءة فقط |

- **متى تستخدم Instances:** لمراجعة إعدادات الاتصال، فترات المزامنة، مواقع FBA، ربط المحاسبة.
- **تحذير:** لا تغيّر حقول Interval أو تفعّل `auto_sync` بدون قراءة القسم 10 (قواعد السلامة).

#### Amazon > Alerts

| القائمة الفرعية | الغرض |
|---|---|
| **Active Alerts** | التنبيهات التشغيلية الحالية (مشاكل اتصال، مهام متعثرة، شذوذ) |
| **Alert History** | التنبيهات السابقة |

- **الإجراءات:** قراءة فقط. التنبيهات تُنشأ تلقائياً.
- **متى تستخدم:** يومياً — للتحقق من وجود مشاكل اتصال أو بيانات.

#### Amazon > Catalog

| القائمة الفرعية | الغرض | الإجراءات |
|---|---|---|
| **Products** | قوائم منتجات Amazon المربوطة بمنتجات Odoo | عرض الربط، التحقق من SKU/ASIN |
| **Initial Product Setup** | معالج إعداد المنتجات لأول مرة | **كتابة** — ينشئ/يربط المنتجات |
| **Import / Map Products** | استيراد أو ربط منتجات Amazon | **كتابة** — ينشئ/يربط المنتجات |

- **متى تستخدم Products:** للتحقق من ربط جميع Amazon SKUs بمنتجات Odoo.
- **متى تستخدم Setup/Import:** فقط عند إضافة منتجات جديدة أو إصلاح SKUs غير مربوطة. ناقش مع فريق العمليات أولاً.

#### Amazon > Orders

| القائمة الفرعية | الغرض | الإجراءات |
|---|---|---|
| **All Orders** | جميع طلبات Amazon المستوردة | عرض الطلبات و Sale Orders المرتبطة في Odoo |
| **FBM Orders** | طلبات Merchant Fulfillment | غير مستخدمة (FBA فقط) |
| **FBA Orders** | طلبات FBA | عرض طلبات FBA |
| **Import Jobs** | سجل مهام استيراد الطلبات | عرض حالة المهمة، الأخطاء، التقدم |
| **Status Sync Jobs** | سجل مهام مزامنة حالة الطلبات | عرض تقدم المزامنة |

- **الأزرار المهمة على الطلبات:** لا يوجد ما يعدّل Amazon. الطلبات هي سجلات للقراءة فقط لما أبلغ عنه Amazon.
- **متى تستخدم:** للتحقق من استيراد الطلبات بشكل صحيح. تحقق من Import Jobs إذا بدت الطلبات ناقصة.

#### Amazon > Delivery

| القائمة الفرعية | الغرض |
|---|---|
| **All Deliveries** | طلبات التسليم في Odoo المرتبطة بطلبات Amazon |
| **Pending / Shipped / Delivered / Cancelled** | عروض مفلترة حسب الحالة |
| **Tracking Numbers** | معلومات تتبع الشحن |

- **ملاحظة:** في FBA، يتولى Amazon التسليم. هذه السجلات تعكس دليل تنفيذ Amazon.

#### Amazon > FBA

هذا هو القسم الأهم لعمليات FBA.

| القائمة الفرعية | الغرض | الإجراءات |
|---|---|---|
| **Inbound Shipments** | إرسال مخزون جديد إلى مستودع Amazon | **كتابة** — ينشئ خطط Inbound، يرسل إلى Amazon |
| **Inventory Health** | مراجعات Audit لمخزون FBA تقارن Odoo مع Amazon | عرض نتائج Audit |
| **FBA Sale Stock Events** | سجلات خصم المخزون الفردية لكل تنفيذ | عرض حالة معالجة الأحداث |
| **Inventory Differences** | مقارنة لكل SKU من Audits | عرض الفروقات |
| **Removal Orders** | طلبات إعادة/التخلص من مخزون FBA | **كتابة** — يمكن إرسال طلبات Removal إلى Amazon |
| **Removal Shipments** | دليل شحن Removals | عرض حالة الشحن |
| **Disposal Orders** | دليل التخلص | عرض |
| **Legacy Inventory Reports** | تنسيق تقارير مخزون قديم | عرض |
| **MCF Outbound Orders** | Multi-Channel Fulfillment | غير مستخدم حالياً |

- **الشاشة الرئيسية — FBA Sale Stock Events:** تعرض كل حدث تنفيذ. كل صف يمثل تنفيذ عنصر طلب واحد. عمود `state` يخبرك إذا تمت المعالجة بنجاح (`done`)، أو في الانتظار (`pending`)، أو يحتاج اهتمام (`manual_review`)، أو فشل (`failed`).
- **الشاشة الرئيسية — Inventory Health:** تعرض مقارنات Audit اليومية. افتح Audit مكتمل لرؤية كميات Odoo مقابل Amazon لكل SKU.

#### Amazon > AI Tools

خصائص مدعومة بالذكاء الاصطناعي (اقتراحات أسعار، تحسين القوائم، التنبؤ بالطلب، إلخ). هذه **اختيارية** وتحتاج AI API Key للإعداد. حالياً **غير مهيأة**.

#### Amazon > Returns & Refunds

| القائمة الفرعية | الغرض | الحالة الحالية |
|---|---|---|
| **Customer Returns** | دليل مرتجعات عملاء FBA | لم يتم الاستيراد بعد |
| **Return Import Runs** | سجل مهام الاستيراد | فارغ |
| **Inventory Adjustments** | تسويات مفقود/تالف/موجود | لم يتم الاستيراد بعد |
| **Reimbursements** | سجلات تعويضات Amazon | لم يتم الاستيراد بعد |

- **الإجراءات:** أزرار الاستيراد **تقرأ** من Amazon. لا تعدّل Amazon.
- **متى تُفعّل:** بعد موافقة العميل. راجع القسم 16.

#### Amazon > Accounting

| القائمة الفرعية | الغرض | الحالة الحالية |
|---|---|---|
| **Settlement Reports** | بيانات Settlement من Amazon | فارغ — لم يتم الاستيراد بعد |
| **Settlement Financial Lines** | تفاصيل Settlement سطراً بسطر | فارغ |
| **Amazon Payouts** | دليل الدفع ومطابقة البنك | فارغ |
| **VCS Tax Reports** | تقارير VAT Calculation Service | غير مهيأ |

- **هذا القسم بالكامل يتطلب إعداد المحاسب قبل الاستخدام.** راجع القسم 14.

#### Amazon > Reports

| القائمة الفرعية | الغرض |
|---|---|
| **Seller Rating** | مقاييس أداء البائع على Amazon |
| **Sync Reports** | ملخص عمليات المزامنة |
| **Sync Logs (Raw)** | سجلات مفصلة لاستدعاءات API |

- **متى تستخدم Sync Logs:** عند استكشاف أخطاء API أو التحقق مما أرسله/استلمه الموديول.

### نموذج Amazon Instance

افتح **Amazon > Configuration > Instances** وانقر على "Amazon Egypt Production". النموذج يحتوي على عدة تبويبات/أقسام:

| القسم | ما يحتويه | الحالة |
|---|---|---|
| **Connection** | بيانات API، Marketplace، Seller ID | مُعدّ |
| **FBA Configuration** | المستودع، مواقع FBA، تاريخ Cutover | مُعدّ |
| **Sync Schedule** | فترات المزامنة و Auto-Sync Toggle | مُعدّ جزئياً (راجع قواعد السلامة) |
| **Settlement / Accounting** | اليوميات، الحسابات، الاستراتيجية | **غير مُعدّ** |
| **AI Features** | AI API Key والفترات | غير مُعدّ |

**الأزرار المهمة على نموذج Instance:**

| الزر | الإجراء | آمن؟ |
|---|---|---|
| **Test Connection** | يختبر اتصال API | نعم — قراءة فقط |
| **Sync Products** | يسحب كتالوج المنتجات من Amazon | آمن غالباً — قد ينشئ سجلات Amazon Product جديدة |
| **Import Orders** | ينشئ مهمة استيراد طلبات | آمن — قراءة فقط من Amazon، ينشئ سجلات في Odoo |
| **Sync Status** | يزامن تحديثات حالة الطلبات | آمن — قراءة فقط من Amazon |
| **Run Audit** | ينشئ FBA Inventory Audit | آمن — لقطة قراءة فقط |
| **Export Stock** | **يدفع مخزون Odoo إلى Amazon** | **خطير — لا تستخدم** |
| **Push Prices** | **يدفع أسعار Odoo إلى Amazon** | **خطير — لا تستخدم** |
| **Full Sync** | يشغّل كل شيء بما فيه الإخراج | **خطير — لا تستخدم** |
| **Import Settlements** | يستورد تقارير Settlement | آمن للقراءة، لكن يحتاج إعداد المحاسبة أولاً |

---

## 3. إعداد Amazon Instance

### إعداد الاتصال

| الحقل | القيمة الحالية | ملاحظات |
|---|---|---|
| Name | Amazon Egypt Production | اسم العرض |
| Marketplace ID | ARBP9OOSHTCHU | Amazon Egypt Marketplace |
| Seller ID | A2LMV58FMUN2ZD | حساب بائع Amazon |
| Refresh Token | مُعدّ | لا تغيّره بدون المطور |
| Client ID (LWA) | مُعدّ | لا تغيّره بدون المطور |
| Client Secret (LWA) | مُعدّ | لا تغيّره بدون المطور |
| Active | نعم | Instance يعمل |
| Company | Eisa Heikal For Commercial & Industrial Investment (id=1) | شركة Odoo |

### إعداد FBA

| الحقل | القيمة الحالية | ملاحظات |
|---|---|---|
| FBA Warehouse | WH (id=1) | مستودع Odoo الرئيسي |
| FBA Source Location | Physical Locations/WH/Stock (id=5) | مصدر الشحنات الصادرة إلى Amazon |
| FBA Transit Location | Amazon FBA Transit (id=29) | في الطريق إلى Amazon |
| FBA Received Location | WH/Stock/Amazon FBA Received / Staging (id=30) | أكد Amazon الاستلام |
| FBA Sellable Location | WH/Stock/Amazon FBA Sellable (id=31) | متاح للبيع على Amazon |
| FBA Reserved Location | WH/Stock/Amazon FBA Reserved (id=32) | محجوز لطلبات قيد الانتظار |
| FBA Unsellable Location | WH/Stock/Amazon FBA Unsellable (id=33) | تالف أو معيب لدى Amazon |
| FBA Return Source | Amazon FBA Customer Returns (id=34) | دليل مرتجعات العملاء |
| FBA Sold / Customers | Amazon FBA Sold / Customers (id=35) | وجهة العناصر المنفّذة |
| FBA Removal Transit | Amazon FBA Removal Transit (id=36) | Removal في الطريق |
| FBA Disposal / Loss | Amazon FBA Disposal / Inventory Loss (id=37) | تم التخلص منه أو فُقد |
| Ship-From Partner | id=1 | الشريك لشحنات Inbound |
| Removal Return Partner | id=1 | الشريك لاستلام Removal |
| FBA Sale Stock Cutover | 2026-09-15 21:02:11 UTC | **لا تغيّره** — راجع القسم 7 |

### مزامنة الطلبات

| الحقل | القيمة الحالية | المعنى | خطورة التغيير |
|---|---|---|---|
| `order_sync_interval` | 30min | كم مرة يقوم Master Scheduler بجدولة استيراد الطلبات | منخفضة (Master Scheduler متوقف) |
| `order_status_sync_enabled` | **True** | Status Sync يعمل مستقلاً كل 15 دقيقة | لا توقفه بدون المطور |
| `order_status_sync_interval` | 15 minutes | كم مرة يتحقق Status Sync من التحديثات | منخفضة — يمكن التعديل عند الحاجة |
| `last_order_sync` | 2026-09-16 13:56:46 | آخر طابع زمني لاستيراد الطلبات الناجح | لا تغيّره يدوياً |
| `last_status_sync_at` | 2026-09-16 17:11:18 | آخر طابع زمني لمزامنة الحالة الناجحة | لا تغيّره يدوياً |
| `initial_order_import_from` | (غير محدد) | يُستخدم فقط لأول استيراد على الإطلاق | غير ذي صلة حالياً |

### مزامنة المنتجات

| الحقل | القيمة الحالية | ملاحظات |
|---|---|---|
| `product_sync_interval` | daily | ستتم المزامنة يومياً لو كان Master Scheduler مفعّلاً (وهو متوقف) |
| `last_product_sync` | 2026-09-14 10:29:25 | آخر مزامنة منتجات |

### مزامنة المخزون

| الحقل | القيمة الحالية | ملاحظات |
|---|---|---|
| `stock_push_interval` | **disabled** | **حماية — يمنع دفع المخزون بالخطأ** |
| `stock_pull_interval` | disabled | سحب المخزون يدوي فقط حسب التصميم |
| `last_stock_sync` | 2026-09-16 13:26:47 | آخر طابع زمني لـ Inventory Audit |

### مزامنة الأسعار

| الحقل | القيمة الحالية | ملاحظات |
|---|---|---|
| `price_push_interval` | **disabled** | **حماية — يمنع دفع الأسعار بالخطأ** |
| `price_pull_interval` | disabled | سحب الأسعار يدوي فقط حسب التصميم |

### مزامنة Settlement

| الحقل | القيمة الحالية | ملاحظات |
|---|---|---|
| `settlement_sync_interval` | **disabled** | **حماية — المحاسبة غير مُعدّة** |
| `settlement_accounting_strategy` | settlement_based | الاستراتيجية المختارة، لكن اليوميات غير مُعدّة |
| `settlement_accounting_cutoff_date` | (غير محدد) | **يجب أن يحدده المحاسب قبل استيراد Settlements** |
| `last_settlement_sync_at` | (لم يحدث) | لم يتم استيراد أي Settlement بعد |

### AI Features

| الحقل | القيمة الحالية |
|---|---|
| AI API Key | غير مُعدّ |
| جميع AI Intervals | weekly (لكن بلا تأثير بدون API Key) |

---

## 4. ربط المنتجات و SKU

### كيف يعمل

كل منتج يُباع على Amazon لديه **SKU** (Seller Stock Keeping Unit) — رمز فريد يعيّنه البائع. كما يعيّن Amazon رقم **ASIN** (Amazon Standard Identification Number) لكل قائمة منتج.

ينشئ الموديول سجل `amazon.product` لكل Amazon SKU. يجب ربط هذا السجل بسجل `product.product` في Odoo لكي تعمل الطلبات والمخزون.

**إذا لم يكن SKU مربوطاً بمنتج Odoo:**
- سيتم استيراد الطلبات التي تحتوي على هذا SKU، لكن سطر الطلب قد يكون غير مكتمل.
- لن تتمكن FBA Sale Stock Events من خصم المخزون للمنتجات غير المربوطة.
- ستعرض Inventory Audits هذا SKU كـ "unmapped".

**إذا كان SKU مربوطاً:**
- تنشئ الطلبات Sale Orders كاملة في Odoo مع المنتج الصحيح.
- تنشئ أحداث التنفيذ Stock Moves من FBA Sellable إلى FBA Sold/Customers.
- يمكن لـ Inventory Audits مقارنة كميات Odoo مع كميات Amazon.

### حالة ربط المنتجات الحالية

جميع منتجات Amazon الـ 19 **مربوطة بالكامل** بمنتجات Odoo. جميعها منتجات IONIC للعناية بالسيارات.

| # | SKU | ASIN | منتج Odoo |
|---|---|---|---|
| 1 | 24-BHT6-LWJ7 | B0BPYF4S5H | IONIC Dashboard Protectant |
| 2 | 6224003090026 | B0DGLR6B23 | IONIC No Rinse Wash & Wax (1kg) |
| 3 | 6225000411319 | B0BPYB4RPR | IONIC Car Shampoo Concentrated |
| 4 | 6225000411388 | B0CHFQGVQR | Ionic Nano Ceramic and Wax |
| 5 | 7C-ADQ8-HTSV | B0HHYVDW2Y | IONIC Microfiber Car Wash Mitt |
| 6 | 7S-2OKY-YSE0 | B0BPYM96H5 | IONIC Interior Detailer |
| 7 | 9A-J2EN-DBFW | B0F5BM9THK | IONIC Microfiber Car Drying Towel |
| 8 | 9J-KM0L-FO5I | B0BPYJR7WQ | IONIC Tire Shine |
| 9 | A6-RDKR-RHZ6 | B0BPYLBBRP | IONIC Waterless Wash & Wax |
| 10 | FD-FUU2-SGIC | B0F5BGGYJJ | IONIC Microfiber Cleaning Towels |
| 11 | IONIC-KIT-CS-01 | B0HHFT3TPJ | IONIC Car Shampoo Wash Kit |
| 12 | IONIC-KIT-NR-01 | B0HHFFC77D | IONIC No Rinse Car Wash Kit |
| 13 | Ion-1110 | B0BZZKFQWR | IONIC No Rinse Wash & Wax |
| 14 | J6-TVWG-OV1I | B0DDLF1VCG | IONIC Car Shampoo |
| 15 | PE-FCJU-3D68 | B0FPG8TY71 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 16 | PX-KEWE-P4MR | B0FPG9SN51 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 17 | UT-6XOE-DDRN | B0FPG9VHQV | IONIC Ultra Thick Microfiber (1200 GSM) |
| 18 | XT-K255-V5H8 | B0BPYK1FKS | IONIC Car Leather Conditioner |
| 19 | XL-ALNF-5LTF | B0FPG9TV9F | IONIC Ultra Thick Microfiber (1200 GSM) |

### SKUs غير المربوطة في Inventory Audits

جميع سجلات `amazon.product` الـ 19 في الموديول مربوطة بالكامل بمنتجات Odoo. ومع ذلك، قد يُرجع API مخزون Amazon أكواد SKU إضافية موجودة على جانب Amazon لكن ليس لها سجل `amazon.product` مقابل في Odoo. تظهر هذه كـ "unmapped" في نتائج Inventory Audit.

**SKUs غير المربوطة الحالية (حتى 2026-09-16):**

| SKU | Amazon Sellable | Amazon Reserved | Amazon Unsellable | Amazon Inbound |
|---|---|---|---|---|
| M7-X72T-G8NN | 0 | 0 | 0 | 0 |
| O8-DZJQ-EDXW | 0 | 0 | 0 | 0 |

كلا الـ SKUs بكمية صفر في جميع فئات مخزون Amazon. قد تكون قوائم Amazon غير نشطة أو محذوفة. **لا تؤثر** على معالجة الطلبات أو خصم المخزون لأنه لا توجد طلبات تشير إليها.

**ما يجب مراقبته:**
- إذا ظهرت كمية غير صفرية لـ SKU غير مربوط، فهذا يعني أن Amazon يحتفظ بمخزون لمنتج لا يعرفه Odoo. حقق فيه واربطه.
- إذا أشار طلب عميل جديد إلى SKU غير مربوط، سيتم استيراد الطلب لكن FBA Sale Stock Event ستدخل حالة `manual_review` مع رمز الخطأ `UNMAPPED_FBA_SKU`.
- استخدم **Amazon > Catalog > Products** للتحقق من حالة الربط.
- استخدم **Amazon > Catalog > Import / Map Products** لربط SKUs جديدة بمنتجات Odoo.

---

## 5. بنية مخزون FBA

### المفهوم

مستودع Amazon FBA يحتفظ بالمخزون الفعلي للبائع. يعكس الموديول هذا في Odoo باستخدام مواقع مخزون خاصة تمثل حالات مختلفة للمخزون لدى Amazon.

**فكّر فيه هكذا:** Odoo لا يتحكم فعلياً في مخزون FBA. بدلاً من ذلك، يحتفظ بـ **نسخة ظلّية** مما يبلّغ عنه Amazon، حتى تتمكن الشركة من رؤية مستويات المخزون، وتتبع الخصم من المبيعات، والمطابقة مع أرقام Amazon الفعلية.

### خريطة مواقع FBA

```
                                ┌─────────────────────┐
Seller Warehouse (WH/Stock) ───→│ Amazon FBA Transit   │ (id=29, transit)
                                │ Stock in transit to  │
                                │ Amazon's warehouse   │
                                └─────────┬───────────┘
                                          ↓
                                ┌─────────────────────┐
                                │ FBA Received/Staging │ (id=30, internal)
                                │ Amazon confirmed     │
                                │ receipt, pending     │
                                │ disposition          │
                                └─────────┬───────────┘
                                          ↓
                    ┌─────────────────────┼──────────────────────┐
                    ↓                     ↓                      ↓
          ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
          │ FBA Sellable    │   │ FBA Reserved     │   │ FBA Unsellable  │
          │ (id=31)         │   │ (id=32)          │   │ (id=33)         │
          │ Available for   │   │ Reserved for     │   │ Damaged or      │
          │ sale on Amazon  │   │ pending orders   │   │ defective       │
          └────────┬────────┘   └──────────────────┘   └─────────────────┘
                   ↓
          ┌─────────────────┐
          │ FBA Sold /      │ (id=35, customer)
          │ Customers       │
          │ Fulfilled and   │
          │ shipped to      │
          │ customer        │
          └─────────────────┘
```

**مواقع إضافية:**

| الموقع | ID | الغرض |
|---|---|---|
| FBA Customer Returns | 34 | موقع دليل مرتجعات العملاء |
| FBA Removal Transit | 36 | المخزون قيد الإعادة من Amazon إلى البائع |
| FBA Disposal / Inventory Loss | 37 | تم التخلص منه أو فُقد بواسطة Amazon |

### ما يعنيه كل موقع

| الموقع | المخزون يصل من | المخزون يذهب إلى | ما يحرّكه |
|---|---|---|---|
| **FBA Transit** (29) | WH/Stock (Dispatch Picking) | FBA Received (Receiving Sync) | إرسال Inbound Shipment ← استلام |
| **FBA Received** (30) | FBA Transit (استلام) | Sellable/Reserved/Unsellable (Reviewed Transfer) | توزيع المخزون بعد المراجعة |
| **FBA Sellable** (31) | FBA Received (توزيع) | FBA Sold/Customers (تنفيذ) | FBA Sale Stock Events (تلقائي) |
| **FBA Reserved** (32) | حركة داخلية في Amazon | يعود إلى Sellable أو يذهب إلى Sold | يديره Amazon؛ ينعكس في Audits |
| **FBA Unsellable** (33) | تلف/عيب لدى Amazon | Removal أو Disposal | يديره Amazon؛ ينعكس في Audits |
| **FBA Sold / Customers** (35) | FBA Sellable (تنفيذ) | — | FBA Sale Stock Events (تلقائي) |
| **FBA Removal Transit** (36) | مخزون FBA (removal) | WH/Stock (استلام فعلي) | دليل Removal Shipment |
| **FBA Disposal** (37) | مخزون FBA (disposal) | — | دليل Disposal |

### المخزون التشغيلي مقابل دليل Amazon

- **المخزون التشغيلي في Odoo** = FBA Sellable + FBA Reserved + FBA Unsellable + FBA Transit + FBA Received
- **تقارير Amazon** = Sellable + Reserved + Unsellable + Inbound (shipped + receiving)

تقارن Inventory Audits اليومية هذه الأرقام. الفروقات الصغيرة طبيعية بسبب التوقيت (طلبات نُفذت بين الاستيراد و Audit). الفروقات الكبيرة المستمرة تتطلب تحقيقاً.

---

## 6. المخزون الافتتاحي / تاريخ التشغيل

### ماذا حدث

في **15 سبتمبر 2026**، بدأ تشغيل تكامل FBA. تم تحميل المخزون الافتتاحي لـ FBA في Odoo بناءً على لقطة من مخزون Amazon FBA الفعلي في تلك اللحظة.

### لقطة الافتتاح

| المرجع | التفاصيل |
|---|---|
| Audit Reference | FBAAUDIT/00007 |
| وقت اللقطة | 2026-09-15 21:02:11 UTC |
| الطريقة | Amazon `getInventorySummaries` API ← تسويات مخزون Odoo القياسية |

### الكميات الافتتاحية

| موقع FBA | الكمية الافتتاحية |
|---|---|
| Sellable | 944 |
| Reserved | 48 |
| Unsellable | 8 |
| Transit | 1,571 |
| **الإجمالي** | **2,571** |

### قواعد مهمة

- هذا المخزون الافتتاحي كان عملية لمرة واحدة. **لا تحمّله مرة أخرى.**
- تم التحقق من الكميات الافتتاحية مقابل API Amazon الحي في ذلك الوقت.
- جميع تغييرات المخزون اللاحقة تأتي من تنفيذ الطلبات، أو استلام Inbound، أو تسويات مراجَعة — ليس أبداً من إعادة استيراد اللقطة الافتتاحية.
- مرجع Audit (FBAAUDIT/00007) هو السجل الدائم لهذا الأساس.

---

## 7. Cutover V2 — شرح للمستخدمين الوظيفيين

### المشكلة التجارية

قبل أن يبدأ Odoo العمل في 15 سبتمبر 2026، كان Amazon قد نفّذ طلبات لمدة عام تقريباً. تلك الطلبات التاريخية كانت قد خصمت بالفعل من المخزون الفعلي لـ FBA.

عندما تم تحميل المخزون الافتتاحي في Odoo (944 وحدة Sellable)، هذا الرقم **يعكس بالفعل** جميع عمليات التنفيذ التاريخية. مثلاً، إذا بدأ Amazon بـ 1,000 وحدة ونفّذ 56 طلباً، فإن اللقطة الافتتاحية أظهرت 944 وحدة متبقية.

**المشكلة:** عندما يستورد Odoo تلك الطلبات القديمة التاريخية ويرى "Amazon نفّذ وحدة واحدة"، سيريد طبيعياً خصم وحدة واحدة من Sellable. لكن ذلك التنفيذ **مضمّن بالفعل** في رقم الافتتاح 944. خصمه مرة أخرى سيكون **حساباً مزدوجاً**، مما يجعل مخزون Odoo غير صحيح.

### كيف يحل Cutover V2 هذه المشكلة

يحتفظ Cutover V2 بـ **Baseline** — سجل بعدد الوحدات التي نفّذها Amazon من كل عنصر طلب قبل تاريخ Cutover. عند استيراد طلب قديم، يتحقق النظام من Baseline ويخصم فقط الكمية **الصافية الجديدة**.

**الحقول الرئيسية في كل Event:**

| الحقل | الاختصار | المعنى |
|---|---|---|
| `amazon_cumulative_fulfilled_qty` | **C** | إجمالي الوحدات التي أبلغ Amazon عن تنفيذها لعنصر الطلب هذا (تراكمي، وليس تزايدي) |
| `cutover_baseline_fulfilled_qty` | **B** | الوحدات التي نفّذها Amazon بالفعل *قبل* التشغيل، من لقطة Baseline |
| `processed_fulfilled_qty` | **P** | الوحدات التي حسبها Odoo بالفعل (إما من تهيئة Baseline أو من عمليات خصم مخزون سابقة) |
| `last_delta_qty` | **D** | آخر كمية خصم مخزون تم تطبيقها |

**كيف يعمل — خطوة بخطوة:**

**الخطوة 1 — إنشاء Event (أول استيراد لعنصر طلب قبل Cutover):**

يبحث النظام عن B من Baseline. ثم **يهيئ P بقيمة B**. هذه هي الخطوة الحرجة: بتعيين P = B عند الإنشاء، يسجل النظام أن B وحدة تم تنفيذها بالفعل قبل التشغيل ولا يجب خصمها مرة أخرى.

**الخطوة 2 — المعالجة (خصم المخزون):**

يحسب المعالج: `D = C - P`

لأن P تمت تهيئته بقيمة B، هذا يعني فعلياً `D = C - B` في المعالجة الأولى. إذا كان D > 0، ينشئ Stock Picking ينقل D وحدة من Sellable إلى Sold/Customers.

بعد المعالجة، يتم تحديث P: `P = P + D` (والتي تساوي الآن C).

**الخطوة 3 — إعادة الاستيراد (Idempotent):**

إذا تم استيراد نفس عنصر الطلب مرة أخرى بنفس C، يعيد المعالج الحساب: `D = C - P`. بما أن P يساوي بالفعل C من المعالجة السابقة، D = 0. لا يحدث شيء.

**الخطوة 4 — تحديث تنفيذ لاحق:**

إذا أبلغ Amazon لاحقاً عن C أعلى (مثلاً، شحنة جزئية اكتملت)، يحسب المعالج D = C_new - P_previous. فقط الوحدات *الجديدة* يتم خصمها.

**مثال — طلب قبل Cutover:**

> عنصر طلب: Amazon نفّذ 5 وحدات إجمالاً (C = 5).
> Baseline: 4 منها تم تنفيذها قبل التشغيل (B = 4).
>
> عند الإنشاء: P يتم تهيئته بـ 4 (= B).
> المعالجة: D = C - P = 5 - 4 = **وحدة واحدة** ← يتم خصم وحدة واحدة من Sellable.
> بعد المعالجة: P = 4 + 1 = 5.
>
> إعادة الاستيراد مع C لا يزال = 5: D = 5 - 5 = **0** ← لا يحدث شيء.

**مثال — طلب بعد Cutover:**

> عنصر طلب جديد: Amazon نفّذ وحدتين (C = 2).
> لا ينطبق Baseline (الطلب بعد Cutover).
>
> عند الإنشاء: P يتم تهيئته بـ 0.
> المعالجة: D = C - P = 2 - 0 = **وحدتان** ← يتم خصم وحدتين من Sellable.
> بعد المعالجة: P = 0 + 2 = 2.

**لماذا هذا مهم:** Baseline (B) لا يُطرح أبداً أثناء المعالجة — يتم استيعابه في P عند إنشاء Event. هذا يعني أن B يُستخدم مرة واحدة فقط. لا يوجد خطر طرح مزدوج.

### حالة Cutover V2 الحالية

| الحقل | القيمة |
|---|---|
| Cutover Run | id=3 |
| State | **activated** |
| History Start | 2025-09-15 21:02:11 UTC (سنة واحدة قبل Cutover) |
| Cutover Timestamp | 2026-09-15 21:02:11 UTC |
| عدد Baselines | **15,269** Baseline لعناصر الطلبات |
| الطلبات الفريدة المغطاة | 13,321 |
| SKUs الفريدة المغطاة | 18 |
| إجمالي المنفّذ قبل Cutover | 16,860 وحدة |

### ما تحتاج معرفته

1. **Cutover يعمل.** تم التحقق منه بطلبات حقيقية وتأكيد أنه يمنع الخصم المزدوج.
2. **الطلبات قبل تاريخ Cutover** (15 سبتمبر 2026 الساعة 21:02:11 UTC) يتم فحصها تلقائياً مقابل Baseline. فقط التنفيذ الصافي الجديد يُخصم.
3. **الطلبات بعد تاريخ Cutover** تُعامل على أنها جديدة 100%. جميع الكميات المنفّذة تُخصم من Sellable.

### حالات Manual Review

أحياناً يُعلّم النظام Event للمراجعة اليدوية بدلاً من معالجتها تلقائياً:

| الحالة | ماذا تعني | ماذا تفعل |
|---|---|---|
| **CUTOVER_BASELINE_OUTSIDE_COVERAGE** | تاريخ شراء طلب قديم يقع خارج نافذة تغطية Baseline (سنة واحدة)، و Baseline يساوي صفر | حقق في الطلب. قد يكون قديماً جداً أو له تواريخ غير عادية. |
| **CUTOVER_BASELINE_EXCEEDS_CUMULATIVE** | Baseline يقول أن وحدات أكثر تم تنفيذها قبل Cutover مما يبلّغ عنه Amazon الآن إجمالياً | شذوذ في البيانات. تحقق إذا كان Amazon عدّل الطلب. |

إذا رأيت Events بحالة `manual_review` في **Amazon > FBA > FBA Sale Stock Events**، حقق قبل إعادة المحاولة. راجع القسم 13 لاستكشاف الأخطاء.

### القواعد الحرجة

> **لا تُعد بناء** Cutover Run 3.
>
> **لا تعدّل** Cutover Timestamp (2026-09-15 21:02:11).
>
> **لا تحذف أو تعدّل** سجلات Baseline.
>
> **لا تنشئ** Cutover Run نشط آخر.
>
> هذه سجلات تاريخية دائمة. تغييرها سيُفسد منطق الخصم لجميع الطلبات.

---

## 8. سير عمل استيراد الطلبات

### كيف تتدفق الطلبات من Amazon إلى Odoo

```
الخطوة 1: يستلم Amazon طلب عميل
                ↓
الخطوة 2: يعمل Order Status Sync Cron كل 15 دقيقة
        يجد الطلبات الجديدة والمحدّثة عبر LastUpdatedAfter
        ينشئ/يحدّث سجلات amazon.sale.order
                ↓
الخطوة 3: لكل طلب:
        → ينشئ أو يحدّث amazon.sale.order (سجل الطلب في Amazon)
        → ينشئ أو يحدّث sale.order (سجل الطلب في Odoo)
        → لكل عنصر FBA:
          → ينشئ أو يحدّث amazon.fba.sale.stock.event
                ↓
الخطوة 4: يعمل Event Processor Cron كل دقيقة واحدة
        لكل Event بحالة pending:
        → يتحقق من Cutover V2 Baseline (إذا كان طلباً قبل Cutover)
        → يحسب Delta (الوحدات الجديدة للخصم)
        → إذا كان Delta > 0: ينشئ Stock Picking
          FBA Sellable → FBA Sold / Customers
        → يعيّن حالة Event إلى 'done'
                ↓
الخطوة 5: يتم خصم مخزون Sellable
```

### آليتان للاستيراد

لدى الموديول طريقتان لاكتشاف الطلبات من Amazon:

**1. Order Import (CreatedAfter) — Cron 24، متوقف حالياً**
- يجد الطلبات **الجديدة** المنشأة منذ آخر استيراد.
- يستخدم `PurchaseDate` للطلب (متى قدّم العميل الطلب).
- تم تفعيل هذا Cron عند بداية التشغيل ثم أُوقف لاحقاً. هو Dispatcher آمن للقراءة فقط ويمكن إعادة تفعيله عند الحاجة.

**2. Order Status Sync (LastUpdatedAfter) — Cron 26، نشط كل 15 دقيقة**
- يجد الطلبات التي **أُنشئت أو تم تحديثها** منذ آخر مزامنة.
- يلتقط الطلبات الجديدة وتغييرات التنفيذ على الطلبات الحالية (مثلاً، شحنات جزئية مكتملة).
- هذه هي **الآلية النشطة حالياً** لاكتشاف الطلبات.

**الحالة الحالية:** Order Status Sync (Cron 26) هو الآلية الأساسية لاكتشاف الطلبات. يلتقط الطلبات الجديدة لأن أي طلب جديد هو أيضاً طلب "محدّث" مؤخراً من منظور Amazon. Order Import المنفصل (Cron 24) يوفر شبكة أمان إضافية باستخدام `CreatedAfter` إذا أُعيد تفعيله.

### حماية من التكرار

- كل طلب Amazon له `amazon_order_ref` فريد. لن ينشئ الموديول طلبات Odoo مكررة لنفس طلب Amazon.
- كل FBA Sale Stock Event فريد بـ `(amazon_order_ref, amazon_order_item_id)`. معالجة نفس عنصر الطلب مرة أخرى تحدّث Event الموجود بدلاً من إنشاء نسخة مكررة.
- يستخدم Event Processor كميات **تراكمية**. إذا قال Amazon "2 وحدة منفّذة" و Odoo عالج بالفعل 1، يتم خصم وحدة واحدة إضافية فقط. إذا قال Amazon لا يزال "2 وحدة منفّذة" عند إعادة الاستيراد، فإن Delta = 0 — لا يحدث شيء.

### التنفيذ الجزئي

إذا نفّذ Amazon طلباً في عدة شحنات:
1. أول استيراد: C=1 ← Odoo يخصم وحدة واحدة.
2. Status Sync لاحقاً: C=2 ← Odoo يخصم وحدة إضافية (Delta = 2-1 = 1).
3. Status Sync مرة أخرى: C=2 ← Delta = 0، لا يحدث شيء.

### الإلغاءات

يتم اكتشاف إلغاءات الطلبات بواسطة Status Sync. الطلب الملغي لا ينشئ خصم مخزون (C=0). إذا تم تنفيذ طلب جزئياً قبل الإلغاء، يتم خصم الكمية المنفّذة فقط.

---

## 9. الأتمتة المشغّلة حالياً

### Crons النشطة (15 إجمالاً)

هذه المهام المجدولة تعمل تلقائياً حالياً:

#### Order Pipeline الأساسي

| ID | الاسم | الفاصل الزمني | الغرض |
|---|---|---|---|
| **25** | Process Order Import Jobs | 1 دقيقة | ينفّذ مهام الاستيراد المجدولة (يستدعي Amazon API، ينشئ الطلبات) |
| **26** | Sync Order Statuses | 15 دقيقة | ينشئ ويعالج مهام Status Sync (يلتقط تحديثات التنفيذ) |
| **27** | Process FBA Sale Stock Events | 1 دقيقة | يعالج أحداث التنفيذ المعلقة (يخصم مخزون Sellable) |

> **ملاحظة:** Cron 24 (Import All Orders) تم تفعيله أثناء بداية التشغيل ثم **أُوقف**. استيراد الطلبات الجديدة يتم حالياً عبر Order Status Sync (Cron 26)، الذي يلتقط الطلبات الجديدة والمحدّثة عبر `LastUpdatedAfter`. إذا احتاج إنتاج استيراد الطلبات للزيادة، يمكن إعادة تفعيل Cron 24 — وهو Dispatcher آمن للقراءة فقط. استشر المطور قبل التغيير.

#### Inbound Operations

| ID | الاسم | الفاصل الزمني | الغرض |
|---|---|---|---|
| **35** | Poll Inbound Operations | 1 دقيقة | يعالج مهام خطط Inbound Shipment |
| **36** | Synchronize Inbound Receiving | 30 دقيقة | يزامن دليل الاستلام من Amazon |

#### مراقبة المخزون

| ID | الاسم | الفاصل الزمني | الغرض |
|---|---|---|---|
| **39** | Enqueue Daily FBA Inventory Audits | يومياً | ينشئ عمليات Audit اليومية |
| **40** | Process FBA Inventory Audits | 5 دقائق | ينفّذ عمليات Audit (يقرأ مخزون Amazon) |

#### البنية التحتية والمراقبة

| ID | الاسم | الفاصل الزمني | الغرض |
|---|---|---|---|
| **46** | Check Connection Health | 15 دقيقة | يختبر اتصال API |
| **47** | Refresh Operations Dashboard | 15 دقيقة | يحدّث إحصائيات Dashboard |
| **48** | Detect Stuck Jobs | 10 دقائق | يُعلّم المهام التي تبدو متعثرة |
| **49** | Dispatch Eligible Retries | 5 دقائق | يعيد محاولة العمليات الفاشلة الآمنة |
| **50** | Evaluate Operational Alerts | 15 دقيقة | يولّد تنبيهات للشذوذ |
| **51** | Clean Successful Operational Logs | يومياً | ينظّف سجلات النجاح القديمة |
| **52** | Process Removal Orders and FBA Events | 5 دقائق | يعالج Removal/Return/Event Jobs |
| **56** | Match FBA Reimbursements | يومياً | يربط سجلات التعويضات |

### Crons المتوقفة (18 إجمالاً)

هذه متوقفة عمداً. **لا تفعّلها بدون قراءة القسم 10.**

#### Order Import Dispatcher (متوقف حالياً)

| ID | الاسم | سبب الإيقاف |
|---|---|---|
| **24** | Import All Orders (FBM + FBA) | تم تفعيله عند بداية التشغيل، ثم أُوقف لاحقاً. Cron آمن للقراءة فقط — يمكن إعادة تفعيله عند الحاجة. استشر المطور. |

#### خطيرة — تكتب إلى Amazon

| ID | الاسم | سبب الإيقاف |
|---|---|---|
| **29** | Update Product Prices | **يكتب الأسعار إلى Amazon** — لا توجد سياسة أسعار معتمدة |
| **33** | Export Stock Levels | **يكتب المخزون إلى Amazon** — مخزون FBA يديره Amazon |
| **42** | Full Bidirectional Sync | **يجمع القراءة والكتابة** — لا يُفعّل أبداً لـ FBA |
| **43** | Master Auto-Sync Scheduler | **يُرسل كل شيء بما فيه الكتابة** — راجع القسم 10 |

#### لم يتم إعدادها بعد

| ID | الاسم | سبب الإيقاف |
|---|---|---|
| **30** | Import Settlement Reports | المحاسبة غير مُعدّة |
| **53** | Import FBA Customer Returns | في انتظار موافقة العميل |
| **54** | Import FBA Inventory Adjustments | في انتظار موافقة العميل |
| **55** | Import FBA Reimbursements | في انتظار موافقة العميل |
| **37** | Refresh Removal Status | في انتظار موافقة العميل |

#### غير قابلة للتطبيق

| ID | الاسم | سبب الإيقاف |
|---|---|---|
| **32** | Import FBM Orders | FBA فقط — لا يوجد Merchant Fulfillment |
| **34** | Update FBM Order Status | FBA فقط |
| **31** | Check Canceled Orders | قديم — مغطى بـ Status Sync |
| **28** | Sync Products | لا توجد سياسة مزامنة منتجات آلية |
| **38** | Enqueue Audits (Compatibility) | تم استبداله بـ Cron 39 |
| **41** | Pull Prices | بلا وظيفة حسب التصميم |
| **44** | Smart Alert Scan | اختياري |
| **45** | Calculate Product Health Scores | اختياري |

---

## 10. قواعد السلامة الحرجة

### الأشياء الثلاثة التي يجب أن لا تحدث أبداً

#### 1. لا تدفع المخزون إلى Amazon

مخزون FBA يديره Amazon. Amazon يعرف ما في مستودعه. دفع أرقام مخزون Odoo إلى Amazon سيستبدل أرقام مخزون Amazon الفعلية بالنسخة الظلية في Odoo، والتي قد لا تكون متطابقة بسبب التوقيت.

**العواقب:** قد يعرض Amazon توفراً غير صحيح، مما يؤدي إلى بيع زائد أو بيع ناقص.

**محمي بواسطة:**
- `stock_push_interval` = **disabled** على Instance 6
- `Export Stock Levels` Cron (id=33) = **متوقف**
- `Full Bidirectional Sync` Cron (id=42) = **متوقف**
- `Master Auto-Sync Scheduler` Cron (id=43) = **متوقف**

#### 2. لا تدفع الأسعار إلى Amazon

لم تتم الموافقة على سياسة دفع أسعار. دفع الأسعار قد يغيّر أسعار قوائم Amazon إلى قيم غير صحيحة.

**محمي بواسطة:**
- `price_push_interval` = **disabled** على Instance 6
- `Update Product Prices` Cron (id=29) = **متوقف**

#### 3. لا تفعّل Master Auto-Sync Scheduler

Master Scheduler (`cron_amazon_master_scheduler`, id=43) هو Dispatcher يعمل كل 15 دقيقة ويشغّل **جميع** عمليات المزامنة للـ Instances التي `auto_sync_enabled` = True فيها. هذا يشمل Stock Push و Price Push الخطيرين.

حتى مع ضبط `stock_push_interval` و `price_push_interval` على `disabled` كحماية، تفعيل Master Scheduler ينشئ مسار مخاطرة غير ضروري.

**محمي بواسطة:**
- `auto_sync_enabled` = **False** على Instance 6
- Master Scheduler Cron (id=43) = **متوقف**

### إعدادات Defense-in-Depth

هذه الحقول الثلاثة تم ضبطها عمداً على `disabled` كطبقة أمان إضافية:

| الحقل | القيمة | الغرض |
|---|---|---|
| `stock_push_interval` | disabled | حتى لو تم تفعيل Master Scheduler بالخطأ، لن يتم دفع المخزون |
| `price_push_interval` | disabled | حتى لو تم تفعيل Master Scheduler بالخطأ، لن يتم دفع الأسعار |
| `settlement_sync_interval` | disabled | يمنع استيراد Settlement قبل إعداد المحاسبة |

### ملخص ضوابط السلامة

| ماذا | الإعداد | Cron | كلاهما مطلوب؟ |
|---|---|---|---|
| Stock Push | `stock_push_interval=disabled` | Export Stock (id=33) متوقف | أيهما يمنعه |
| Price Push | `price_push_interval=disabled` | Update Prices (id=29) متوقف | أيهما يمنعه |
| Master Dispatch | `auto_sync_enabled=False` | Master Scheduler (id=43) متوقف | أيهما يمنعه |
| Full Sync | غير متاح | Full Sync (id=42) متوقف | يجب أن يكون Cron نشطاً |
| Settlement Import | `settlement_sync_interval=disabled` | Import Settlement (id=30) متوقف | أيهما يمنعه |

---

## 11. حالة الإنتاج الحالية

*لقطة مأخوذة: 2026-09-16 17:15 UTC*

### أعداد الطلبات و Events

| المقياس | العدد |
|---|---|
| Amazon Orders المستوردة | 43 |
| Odoo Sale Orders | 43 |
| FBA Sale Stock Events (done) | 55 |
| FBA Sale Stock Events (pending) | 0 |
| FBA Sale Stock Events (manual_review) | 0 |
| FBA Sale Stock Events (failed) | 0 |
| Stock Moves | 82 |
| Stock Pickings | 33 |
| Import Jobs (إجمالي / فاشلة) | 11 / 0 |
| Status Sync Jobs (إجمالي / فاشلة) | 23 / 0 |

### مخزون FBA

| الموقع | الكمية |
|---|---|
| Sellable | 902 |
| Reserved | 48 |
| Unsellable | 8 |
| Transit | 1,571 |
| **الإجمالي** | **2,529** |

### مطابقة الخصم

| العنصر | القيمة |
|---|---|
| Sellable الافتتاحي | 944 |
| إجمالي المخصوم (Sum of D) | 42 |
| Sellable المتوقع | 944 − 42 = 902 |
| Sellable الفعلي | 902 |
| **متطابق** | **نعم** |

هذه المطابقة تؤكد الاتساق الداخلي: كل وحدة خُصمت من Sellable محسوبة بواسطة FBA Sale Stock Event معالَج. هذا **لا يعني** أن Sellable في Odoo سيطابق دائماً بالضبط عدد Sellable الحي في Amazon — راجع "تفسير Audit" أدناه.

### آخر Inventory Audit

| Audit | FBAAUDIT/00011 |
|---|---|
| التاريخ | 2026-09-16 13:26 UTC |
| سجلات Amazon المقروءة | 20 |
| متطابقة | 4 |
| غير متطابقة | 14 |
| غير مربوطة | 2 |
| لم تُعاد | 1 |

### تفسير Audit

**مطابقة الخصم** (944 − sum(D) = Sellable الحالي) تثبت أن المحاسبة *الداخلية* للمخزون في Odoo صحيحة — كل Event تم معالجته مرة واحدة بالضبط مع Delta الصحيح.

**عدم تطابق Audit** يقارن كميات Odoo لكل SKU مع *لقطة مخزون Amazon الحية*. الفروقات المؤقتة متوقعة لأن:

- طلبات نفّذها Amazon بين آخر استيراد ولقطة Audit تقلل عدد Amazon لكن ليس بعد عدد Odoo.
- قد يعيد Amazon تصنيف المخزون بين Sellable و Reserved لطلبات قيد التحضير.
- تسويات المخزون من جانب Amazon (مفقود، موجود، تالف) لم يتم استيرادها بعد في Odoo.

الـ 14 عدم تطابق في FBAAUDIT/00011 هي في الغالب فروقات في توزيع Sellable/Reserved من هذا التأثير الزمني. الـ 2 SKU غير المربوطة (M7-X72T-G8NN، O8-DZJQ-EDXW) هي قوائم من جانب Amazon بكمية صفر — راجع القسم 4 للتفاصيل.

**متى يجب التحقيق:** إذا أظهر نفس SKU عدم تطابق مستمر ومتزايد عبر عدة عمليات Audit متتالية، أو إذا تجاوزت فجوة Sellable الإجمالية 50 وحدة، صعّد إلى المطور.

---

## 12. المراقبة والعمليات اليومية

### الفحص اليومي (5 دقائق)

| # | ماذا تتحقق | أين | ما هو الطبيعي | صعّد إذا |
|---|---|---|---|---|
| 1 | صحة الاتصال | Amazon > Dashboard أو Alerts | أخضر / سليم | أخطاء اتصال تستمر > ساعة واحدة |
| 2 | مهام استيراد الطلبات | Amazon > Orders > Import Jobs | مهام حديثة بحالة "done" | مهام عالقة في "running" أو بحالة "failed" |
| 3 | FBA Sale Stock Events | Amazon > FBA > FBA Sale Stock Events، فلتر بالحالة | جميعها "done" | أي Events بحالة "manual_review" أو "failed" |
| 4 | التنبيهات التشغيلية | Amazon > Alerts > Active Alerts | صفر أو تنبيهات معلوماتية | تنبيهات حرجة |
| 5 | اتجاه Sellable | Amazon > FBA > Inventory Health | انخفاض تدريجي يتناسب مع المبيعات | انخفاضات أو ارتفاعات كبيرة مفاجئة |

### الفحص الأسبوعي (15 دقيقة)

| # | ماذا تتحقق | أين | ماذا تبحث عنه |
|---|---|---|---|
| 1 | مقارنة Inventory Audit | Amazon > FBA > Inventory Health | افتح آخر Audit مكتمل. فجوة Sellable يجب أن تكون صغيرة (< 30 وحدة). |
| 2 | SKUs غير مربوطة | Amazon > Catalog > Products | أي Amazon SKUs جديدة بدون ربط بمنتجات Odoo. اربطها. |
| 3 | مهام متعثرة | Amazon > Operations > Job Monitor | مهام أقدم من 24 ساعة في حالة غير نهائية. |
| 4 | Sync Logs | Amazon > Reports > Sync Logs | أخطاء متكررة أو تحذيرات Throttling. |
| 5 | إحصائيات Events | فلتر Events حسب التاريخ | هل يتم إنشاء Events ومعالجتها بشكل متسق؟ |

### ما هو طبيعي

- **فجوات Sellable صغيرة** (Odoo > Amazon بـ 5-20 وحدة): طبيعي. طلبات نُفذت بين دورات الاستيراد.
- **فروقات Reserved**: طبيعي. Amazon يحجز المخزون لطلبات قيد التحضير.
- **مهام الاستيراد تكتمل في 1-3 دفعات**: طبيعي لـ ~50 طلب/يوم.
- **Status Sync يعمل كل 15 دقيقة**: طبيعي ومتوقع.

### ما يتطلب تصعيداً

| العَرَض | الخطورة | الإجراء |
|---|---|---|
| Event بحالة `manual_review` | متوسطة | حقق في الطلب المحدد. راجع استكشاف الأخطاء. |
| مهمة استيراد فاشلة | عالية | تحقق من رسالة الخطأ. قد تكون مشكلة API. |
| فجوة Sellable > 50 وحدة | عالية | شغّل Audit يدوياً وقارن. |
| فشل صحة الاتصال | حرجة | تحقق من بيانات API. اتصل بالمطور. |
| Cron لا يعمل | حرجة | تحقق من Settings > Technical > Scheduled Actions. |

---

## 13. دليل استكشاف الأخطاء

### توقف استيراد الطلبات

**الأعراض:** لا تظهر طلبات جديدة. مهام Status Sync لا تُظهر نشاطاً حديثاً.

**أين تبحث:**
- Settings > Technical > Scheduled Actions > "Amazon: Sync Order Statuses" (id=26) — هل هو نشط؟
- Amazon > Orders > Status Sync Jobs — هل هناك مهمة عالقة أو فاشلة؟
- Amazon > Alerts — أي أخطاء اتصال؟

**إجراءات آمنة:**
- تحقق أن Cron 26 (Sync Order Statuses) نشط.
- تحقق من حالة آخر مهمة Status Sync ورسالة الخطأ.
- انقر "Import Orders" في نموذج Instance لتشغيل استيراد يدوي.
- إذا كان Cron 24 (Import All Orders) مطلوباً لتغطية إضافية، يمكن إعادة تفعيله بأمان — استشر المطور.

**متى تتصل بالمطور:** إذا كان Cron نشطاً لكن المهام لا تُنشأ، أو إذا فشلت المهام بأخطاء تقنية.

### الطلب موجود في Amazon لكن ليس في Odoo

**تحقق من:**
1. هل الطلب ضمن نافذة الاستيراد؟ تحقق من `last_order_sync` في Instance.
2. هل تم استيراد الطلب بمرجع مختلف؟ ابحث بـ Amazon Order ID.
3. هل هناك مهمة استيراد فاشلة ربما تخطته؟

**إجراء آمن:** انقر "Import Orders" في نموذج Instance. الاستيراد التالي سيشمل أي طلبات ناقصة بعد `last_order_sync`.

### الطلب مستورَد لكن المخزون لم ينقص

**تحقق من:**
1. Amazon > FBA > FBA Sale Stock Events — ابحث عن Event لعنصر الطلب هذا.
2. هل حالة Event هي "done"؟ ← يجب أن يكون المخزون قد نقص. تحقق من Picking.
3. هل حالة Event هي "pending"؟ ← انتظر Processor Cron (يعمل كل دقيقة).
4. هل حالة Event هي "manual_review"؟ ← راجع أدناه.
5. هل Event مفقود؟ ← عنصر الطلب قد لا يكون FBA، أو المنتج قد يكون غير مربوط.

**تحقق من تفاصيل Event:**
- `amazon_cumulative_fulfilled_qty` (C) — ما يبلّغ عنه Amazon كمنفّذ
- `processed_fulfilled_qty` (P) — ما عالجه Odoo بالفعل
- `last_delta_qty` (D) — آخر كمية خصم

إذا C=0، Amazon لم ينفّذ هذا العنصر بعد. لا يُتوقع خصم.

### خصم المخزون مرتين (خصم مزدوج)

هذا **يجب ألا** يحدث إذا كان Cutover V2 يعمل بشكل صحيح.

**تحقق من:**
1. هل توجد Events مكررة لنفس عنصر الطلب (نفس `amazon_order_ref` + `amazon_order_item_id`).
2. تحقق من قيمة B (Baseline) في Event للطلبات قبل Cutover.

**متى تتصل بالمطور:** فوراً. هذه مشكلة سلامة بيانات حرجة.

### Event عالق في حالة `pending`

**تحقق من:**
1. هل `Process FBA Sale Stock Events` Cron نشط؟ (id=27)
2. هل لدى Event قيمة `next_run_at` في المستقبل؟
3. هل المنتج مربوط و Stockable؟

**إجراء آمن:** افتح Event. إذا بدا صحيحاً، انقر "Retry" لإعادة جدولته.

### Event في حالة `manual_review`

**تحقق من رمز الخطأ:**

| رمز الخطأ | المعنى | الإجراء |
|---|---|---|
| `CUTOVER_BASELINE_OUTSIDE_COVERAGE` | طلب قديم خارج نافذة تغطية Baseline (سنة واحدة) مع B=0 | تحقق من تاريخ الطلب. إذا كان تاريخياً فعلاً، فالعناصر تم تنفيذها قبل فترة تغطية Baseline. قد يحتاج المطور لإضافة Baseline يدوي. |
| `CUTOVER_BASELINE_EXCEEDS_CUMULATIVE` | B > C — Baseline يقول أن وحدات أكثر تم تنفيذها قبل Cutover مما يبلّغ عنه Amazon الآن إجمالاً | شذوذ في البيانات. Amazon قد يكون عدّل الطلب. حقق في الطلب على Amazon Seller Central. |

**إجراء آمن:** لا تعد المحاولة بدون فهم السبب. وثّق مرجع الطلب واتصل بالمطور.

### عدم تطابق مخزون Amazon/Odoo في Audit

**أسباب طبيعية:**
- طلبات نُفذت بين الاستيراد و Audit (Odoo > Amazon بعدة وحدات)
- فروقات توزيع Reserved

**أسباب غير طبيعية (حقق فيها):**
- عدم تطابق مستمر يتزايد مع الوقت
- Odoo يعرض مخزوناً أقل بكثير من Amazon
- SKU محدد غير متطابق باستمرار

**إجراء آمن:** شغّل Audit جديد (Amazon > FBA > Inventory Health > New). قارن مع Audits السابقة. إذا كانت الفجوة تتزايد، صعّد.

### SKU غير مربوط في Audit

**الأعراض:** Audit يعرض حالة "unmapped" لـ SKU.

**الإجراء:**
1. اذهب إلى Amazon > Catalog > Products.
2. ابحث عن SKU.
3. إذا كان موجوداً لكن بدون ربط بمنتج Odoo، استخدم وظيفة Link.
4. إذا لم يكن موجوداً، استخدم Import / Map Products لسحبه من Amazon.

### تجاوز حصة Amazon API

**الأعراض:** Sync Logs تعرض HTTP 429 (Too Many Requests) أو رسائل Throttling.

**الإجراء:** هذا مؤقت. الموديول لديه منطق إعادة محاولة مدمج. انتظر 15-30 دقيقة وتحقق إذا استُؤنفت العمليات. إذا استمر Throttling لساعات، قلل تكرار المزامنة أو اتصل بالمطور.

### Cron لا يعمل

**تحقق من:** Settings > Technical > Scheduled Actions. ابحث عن "Amazon". تحقق من خانة Active و Last Execution Date.

**إذا كان Cron متوقفاً ويجب أن يكون نشطاً:** راجع قائمة Crons النشطة في القسم 9. فعّل فقط Crons من القائمة "النشطة".

---

## 14. تسليم المحاسبة و Settlement

### الحالة الحالية

**تكامل Order و FBA Stock يعمل.** إعداد المحاسبة و Settlement **لم يكتمل بعد.**

لم يتم استيراد أي Settlement Reports. لم يتم إنشاء أي قيود محاسبية. لم تتم أي مطابقة Payout.

### ما يحتاج إعداده

| # | الإعداد | الحالة الحالية | القرار المطلوب | من يقرر | يمنع التقدم؟ |
|---|---|---|---|---|---|
| 1 | Settlement Journal | **غير محدد** | إنشاء أو اختيار يومية لقيود Amazon Settlement | المحاسب + Implementer | نعم — يمنع محاسبة Settlement |
| 2 | Amazon Payout Bank Journal | **غير محدد** | اختيار يومية البنك التي تصل إليها إيداعات Amazon | المحاسب | نعم — يمنع مطابقة Payout |
| 3 | Amazon Clearing Account | **غير محدد** | إنشاء أو اختيار حساب Clearing/Suspense لدورة Amazon Payout | المحاسب | نعم — يمنع محاسبة Settlement |
| 4 | Settlement Cutoff Date | **غير محدد** | اختيار التاريخ الذي يبدأ منه إنشاء القيود المحاسبية من Settlements | المحاسب + العميل | نعم — يمنع استيراد Settlement |
| 5 | Settlement Accounting Strategy | `settlement_based` | تأكيد صحة هذا (مقابل invoice-aware) | المحاسب | تأكيد فقط |
| 6 | Amazon Sales Account | **غير محدد** | حساب الإيرادات لمبيعات Amazon | المحاسب | نعم |
| 7 | Amazon Fee Account | **غير محدد** | حساب المصروفات لرسوم Amazon Referral/Commission | المحاسب | نعم |
| 8 | Amazon FBA Fee Account | **غير محدد** | حساب المصروفات لرسوم FBA Fulfillment | المحاسب | نعم |
| 9 | Amazon Refund Account | **غير محدد** | حساب مرتجعات العملاء | المحاسب | نعم |
| 10 | Amazon Reimbursement Account | **غير محدد** | حساب تعويضات Amazon (مخزون مفقود/تالف) | المحاسب | نعم |
| 11 | Amazon Shipping Account | **غير محدد** | حساب رسوم/ائتمانات الشحن | المحاسب | نعم |
| 12 | Amazon Promotion Account | **غير محدد** | حساب خصومات الترويج | المحاسب | نعم |
| 13 | Amazon Tax Account | **غير محدد** | حساب مبالغ الضرائب | المحاسب | نعم |
| 14 | Amazon Adjustment Account | **غير محدد** | حساب التسويات المتنوعة | المحاسب | نعم |
| 15 | Amazon Other Credit Account | **غير محدد** | حساب شامل للائتمانات غير المصنفة | المحاسب | نعم |
| 16 | Amazon Other Debit Account | **غير محدد** | حساب شامل للمدينات غير المصنفة | المحاسب | نعم |
| 17 | Amazon Suspense Account | **غير محدد** | حساب احتجاز مؤقت للبنود غير القابلة للتصنيف | المحاسب | نعم |
| 18 | Egyptian Tax Configuration | **غير مُعدّ** | سلوك شامل الضريبة، ربط الضرائب، معاملة VAT | المحاسب + مستشار الضرائب | نعم |

### ما غرض كل حساب

**Settlement Journal:** اليومية المحاسبية التي تُسجل فيها قيود Amazon Settlement (مثلاً "Amazon Egypt Settlements").

**Amazon Clearing Account:** حساب ميزانية يمثل "الأموال المستحقة لنا من Amazon". قيود Settlement تُقيّد هذا الحساب بالإيرادات وتخصمه بالرسوم. عندما يصل التحويل البنكي من Amazon، يتم تصفية هذا الحساب.

**Amazon Sales Account:** حساب إيرادات (قائمة الدخل) لمبلغ بيع المنتج.

**Amazon Fee Account:** حساب مصروفات لرسوم Amazon Referral (نسبة من سعر البيع).

**Amazon FBA Fee Account:** حساب مصروفات لرسوم Amazon Fulfillment (التجهيز، التعبئة، الشحن).

**Amazon Refund Account:** حساب إيرادات عكسي أو مصروفات لمرتجعات العملاء التي يعالجها Amazon.

**Amazon Reimbursement Account:** حساب إيرادات للتعويضات التي يدفعها Amazon عند فقدان أو تلف مخزون البائع.

**Amazon Shipping Account:** حساب لرسوم وائتمانات الشحن في Settlements.

**Amazon Promotion Account:** حساب لخصومات الترويج التي يطبقها Amazon.

**Amazon Tax Account:** حساب لمبالغ الضرائب المحصّلة والمحولة.

**Amazon Payout Bank Journal:** يومية البنك في Odoo التي يصل إليها التحويل البنكي الفعلي من Amazon. تُستخدم لمطابقة Clearing Account.

### سير العمل المحاسبي

```
Amazon Settlement Report
        ↓
استيراد في Odoo (بيانات للقراءة فقط)
        ↓
مراجعة Settlement Lines
(إيرادات، رسوم، مرتجعات، تعويضات، ضرائب، تسويات)
        ↓
إنشاء قيد محاسبي (مسودة فقط)
        ↓
المحاسب يراجع قيد اليومية المسودة
        ↓
المحاسب يرحّل القيد (إجراء Odoo القياسي)
        ↓
Clearing Account الآن لديه رصيد
        ↓
يصل التحويل البنكي من Amazon
        ↓
تسجيل Payout / مطابقة المعاملة البنكية
        ↓
مطابقة Clearing Account
```

**مهم:** إجراء "Create Accounting Entry" ينشئ قيد يومية **مسودة**. لا يرحّل تلقائياً. يجب أن يراجع المحاسب ويرحّل يدوياً باستخدام سير عمل محاسبة Odoo القياسي.

---

## 15. إجراء اختبار أول Settlement

**هذا إجراء لعندما يكون المحاسب جاهزاً. لا تنفذه الآن.**

### المتطلبات المسبقة

يجب إعداد جميع العناصر في القسم 14 أولاً.

### خطوة بخطوة

1. **أعدّ جميع Account Mappings** في نموذج Amazon Instance (القسم 14).
2. **حدد Settlement Cutoff Date** — عادة تاريخ التشغيل (2026-09-15) أو التاريخ المتفق عليه مع المحاسب.
3. **استورد Settlement واحد يدوياً:**
   - اذهب إلى Amazon > Configuration > Instances > Amazon Egypt Production.
   - انقر زر "Import Settlements".
   - هذا يقرأ بيانات Settlement من Amazon — **لا** ينشئ قيوداً محاسبية.
4. **راجع Settlement المستورد:**
   - اذهب إلى Amazon > Accounting > Settlement Reports.
   - افتح Settlement المستورد.
   - راجع Settlement Lines (Amazon > Accounting > Settlement Financial Lines).
   - تحقق من: مبالغ الإيرادات، فئات الرسوم، المرتجعات، الضرائب، الإجمالي يطابق تقرير Amazon.
5. **أنشئ قيد محاسبي مسودة:**
   - على Settlement Report، انقر "Create Accounting Entry" (إن وُجد).
   - هذا ينشئ قيد يومية مسودة — **لم يُرحَّل بعد**.
6. **مراجعة المحاسب:**
   - افتح قيد اليومية المسودة.
   - تحقق من كل سطر: إيرادات، Referral Fees، FBA Fees، مرتجعات، ضرائب، رصيد Clearing.
   - قارن الإجمالي مع مبلغ "Total" في تقرير Amazon Settlement.
7. **إذا كان صحيحاً:** المحاسب يرحّل القيد باستخدام زر "Post" القياسي في Odoo.
8. **إذا كان غير صحيح:** أصلح Account Mappings، احذف القيد المسودة، وأعد من الخطوة 5.
9. **مطابقة Payout:**
   - عندما يصل التحويل البنكي من Amazon، سجّله في Bank Journal.
   - طابق المعاملة البنكية مع رصيد Amazon Clearing Account.
10. **كرر** مع 2-3 Settlements أخرى قبل التفكير في الأتمتة.
11. **فقط بعد موافقة مكتوبة:** فكّر في تفعيل `Import Settlement Reports` Cron (id=30) وضبط `settlement_sync_interval` على `daily`.

### ما لا يجب فعله

- لا تفعّل Settlement Import Cron قبل إعداد جميع الحسابات.
- لا ترحّل قيوداً محاسبية بدون مراجعة المحاسب.
- لا تفترض أن أول استيراد سيكون لديه Account Mappings صحيحة — اختبر وعدّل.

---

## 16. Returns / Removals / Reimbursements

هذه الخصائص تستورد أدلة من Amazon. لا شيء منها نشط حالياً.

### Customer Returns

| الجانب | التفاصيل |
|---|---|
| **ما يُستورد** | سجلات المنتجات المُرجعة من العملاء إلى مستودع Amazon FBA |
| **هل يغيّر مخزون Odoo؟** | **لا** — المرتجعات دليل معلوماتي فقط. المخزون المُرجع يذهب إلى مستودع Amazon FBA، وليس إلى مستودع البائع في Odoo. |
| **هل يغيّر المحاسبة؟** | لا — محاسبة المرتجعات تأتي من Settlements، وليس من Returns |
| **Cron الحالي** | `Import FBA Customer Returns` (id=53) — **متوقف** |
| **البيانات المستوردة حتى الآن** | 0 Return Reports |
| **القرار التجاري المطلوب** | يجب أن يفهم العميل أن Returns ≠ إضافة مخزون تلقائية |
| **متى تُفعّل** | بعد موافقة العميل، فعّل Cron |

### Inventory Adjustments

| الجانب | التفاصيل |
|---|---|
| **ما يُستورد** | سجلات المخزون المفقود أو التالف أو الموجود أو المُعدّل في مستودع Amazon |
| **هل يغيّر مخزون Odoo؟** | يعتمد على السياسة. حالياً محدد كـ `informational` — يستورد البيانات فقط، لا ينقل المخزون |
| **هل يغيّر المحاسبة؟** | لا |
| **Cron الحالي** | `Import FBA Inventory Adjustments` (id=54) — **متوقف** |
| **البيانات المستوردة حتى الآن** | 0 Adjustments |
| **القرار التجاري المطلوب** | تأكيد أن سياسة `informational` صحيحة، أو تقرير إذا كان يجب أن تنشئ Adjustments حركات مخزون |
| **متى تُفعّل** | بعد تأكيد العميل للسياسة |

### Reimbursements

| الجانب | التفاصيل |
|---|---|
| **ما يُستورد** | سجلات تعويض Amazon للبائع عن مخزون FBA المفقود/التالف |
| **هل يغيّر مخزون Odoo؟** | لا |
| **هل يغيّر المحاسبة؟** | ليس مباشرة — مبالغ التعويض تظهر في Settlements |
| **Crons الحالية** | `Import FBA Reimbursements` (id=55) — **متوقف**؛ `Match FBA Reimbursements` (id=56) — **نشط** (يربط السجلات) |
| **البيانات المستوردة حتى الآن** | 0 Reimbursements |
| **القرار التجاري المطلوب** | موافقة العميل لبدء الاستيراد |
| **متى تُفعّل** | بعد موافقة العميل |

### Removal Orders

| الجانب | التفاصيل |
|---|---|
| **ما يفعله** | يتيح طلب من Amazon إعادة شحن مخزون FBA أو التخلص منه. يتتبع حالة Removal Order ودليل الشحن. |
| **هل يغيّر مخزون Odoo؟** | نعم، عند معالجة Removal Shipment — ينقل المخزون من مواقع FBA إلى Removal Transit أو Disposal |
| **هل يغيّر المحاسبة؟** | ليس مباشرة |
| **Cron الحالي** | `Refresh Removal Status` (id=37) — **متوقف** |
| **البيانات المستوردة حتى الآن** | 0 Removal Orders |
| **القرار التجاري المطلوب** | يجب أن يكون لدى العميل Removal Orders لتتبعها |
| **متى تُفعّل** | عندما يبدأ العميل باستخدام FBA Removals |

---

## 17. شحنات FBA الواردة

### نظرة عامة

عندما يحتاج البائع لإرسال مخزون جديد إلى مستودع Amazon FBA، يدير الموديول سير عمل Inbound بالكامل.

### سير عمل Inbound

```
1. إنشاء Inbound Plan (Amazon > FBA > Inbound Shipments)
   → تبلّغ Amazon بالمنتجات والكميات المراد إرسالها

2. توليد Packing Options
   → Amazon يقترح كيفية التعبئة

3. تأكيد Packing + ضبط معلومات التعبئة
   → تحديد أبعاد الصناديق والمحتويات

4. توليد Placement Options
   → Amazon يحدد مراكز التنفيذ للشحن إليها

5. تأكيد Placement
   → قبول التوزيع (قد يشمل وجهات متعددة)

6. توليد Transportation
   → اختيار طريقة الشحن

7. تأكيد Transportation
   → إنهاء ترتيبات الشحن

8. طباعة Labels (ملصقات المنتج + ملصقات الصناديق)
   → تطبيقها على الصناديق الفعلية

9. تقديم Tracking (للشحن الذاتي)
   → إدخال أرقام تتبع الناقل

10. إنشاء Dispatch Picking
    → ينشئ Stock Picking في Odoo: WH/Stock → FBA Transit

11. تأكيد Dispatch
    → البضائع الفعلية تغادر المستودع. المخزون ينتقل إلى FBA Transit.

12. Receiving Sync (تلقائي، كل 30 دقيقة)
    → Amazon يؤكد الاستلام. المخزون ينتقل من Transit إلى Received/Staging.

13. Inventory Disposition (بعد المراجعة)
    → المخزون ينتقل من Received إلى Sellable/Reserved/Unsellable
```

### Crons الـ Inbound النشطة حالياً

- **Poll Inbound Operations** (id=35) — 1 دقيقة — يعالج مهام خطط Inbound
- **Synchronize Inbound Receiving** (id=36) — 30 دقيقة — يزامن دليل الاستلام

### ملاحظات مهمة

- الخطوات 1-9 تتطلب **إجراء يدوي** من فريق العمليات. الموديول يوفر الواجهة لكن لا يؤتمت القرارات التجارية.
- الخطوة 10 (Dispatch Picking) تغيّر مخزون Odoo. أكّد فقط عند مغادرة البضائع فعلياً.
- الخطوة 12 (Receiving) تلقائية — Cron يتحقق من Amazon لتأكيد الاستلام.
- Ship-From Partner (id=1) و Removal Return Partner (id=1) مُعدّان.

---

## 18. قرارات سياسة المنتج / السعر

يجب اتخاذ هذه القرارات من العميل وتوثيقها قبل تفعيل الأتمتة المتعلقة.

| # | القرار | الخيارات | الأثر | الحالة الحالية |
|---|---|---|---|---|
| 1 | من هو المرجع الرئيسي للمنتج؟ | **Odoo** (إدارة القوائم محلياً، دفع إلى Amazon) أو **Amazon** (إدارة في Seller Central، سحب إلى Odoo) | يحدد اتجاه المزامنة | لم يُقرر |
| 2 | هل يجب أن تُنشئ Amazon SKUs الجديدة منتجات Odoo تلقائياً؟ | **نعم** (Sync Products Cron ينشئها) أو **لا** (ربط يدوي فقط) | يؤثر على التعامل مع SKU غير المربوط | حالياً يدوي |
| 3 | هل يجب أن تعمل مزامنة المنتجات تلقائياً؟ | **نعم** (فعّل Cron id=28) أو **لا** (يدوي فقط) | يحدد إذا كانت القوائم الجديدة تظهر تلقائياً في Odoo | حالياً يدوي |
| 4 | هل يجب دفع الأسعار من Odoo إلى Amazon؟ | **نعم** (فعّل Price Push) أو **لا** (إدارة الأسعار على Amazon فقط) | كبير — أسعار خاطئة قد تؤثر على الإيرادات | حالياً متوقف |
| 5 | ماذا يحدث للـ SKUs غير المربوطة؟ | **تنبيه** + **ربط يدوي** أو **إنشاء تلقائي** | يؤثر على اكتمال استيراد الطلبات | حالياً: SKUs غير مربوطة مذكورة في Audits |
| 6 | هل يجب أن يدفع Odoo مستويات المخزون إلى Amazon؟ | شبه مؤكد **لا** لـ FBA | دفع مخزون غير صحيح قد يسبب بيعاً زائداً | متوقف ومحمي |

**إجراء Implementer:** راجع هذه القرارات مع العميل. وثّق الإجابات. فقط بعدها فكّر في تفعيل الأتمتة المتعلقة.

---

## 19. مصفوفة المسؤوليات

| المجال | المطور | Implementer | المحاسب | عمليات العميل |
|---|---|---|---|---|
| بيانات API والاتصال | يُعدّ | يراقب الصحة | — | — |
| ربط Product SKU | — | يربط المنتجات، يحل غير المربوطة | — | يوفر سياسة SKU |
| Chart of Accounts | — | يساعد في الإعداد | **يقرر وينشئ** | — |
| Settlement Account Mappings | — | يُعدّ في Odoo | **يقرر الحسابات** | — |
| إعداد الضرائب المصرية | — | يُعدّ في Odoo | **يقرر المعاملة الضريبية** | — |
| التحقق من Settlement | — | يستورد البيانات | **يراجع ويرحّل** | — |
| مطابقة Payout | — | يساعد | **يطابق** | — |
| Cutover V2 | **يصون — لا تلمسه** | — | — | — |
| إدارة Cron (السلامة) | **يوافق على التغييرات** | يراقب | — | — |
| المراقبة اليومية | — | **الأساسي** | — | يراجع التقارير |
| Inbound Shipments | — | يساعد أول مرة | — | **يخطط وينفذ** |
| سياسة الأسعار | — | يُعدّ إذا تمت الموافقة | — | **يقرر** |
| سياسة Return/Removal/Adjustment | — | يفعّل بعد الموافقة | — | **يقرر** |
| إصلاح الأخطاء وتغييرات الكود | **يتولى** | يبلّغ عن المشاكل | — | يبلّغ عن المشاكل |

---

## 20. قائمة الممنوعات

هذه العناصر حرجة لسلامة النظام المشغّل. لا تعدّلها بدون موافقة المطور.

| # | العنصر | الموقع | عاقبة التغيير |
|---|---|---|---|
| 1 | **Cutover Run 3** | `amazon.fba.sale.stock.cutover.run` id=3 | يُفسد منع الخصم المزدوج لجميع الطلبات التاريخية |
| 2 | **Cutover Timestamp** | `fba_sale_stock_cutover_at` على Instance 6: `2026-09-15 21:02:11` | يغيّر أي الطلبات تُعامل كتاريخية مقابل حية |
| 3 | **History Start Date** | `history_start_at` على Cutover Run 3: `2025-09-15 21:02:11` | يغيّر نافذة تغطية Baseline |
| 4 | **15,269 سجل Baseline** | `amazon.fba.sale.stock.cutover.baseline` | يُفسد حسابات الخصم لكل طلب |
| 5 | **FBA Location IDs** (29-37) | `stock.location` | يكسر كل منطق حركة المخزون |
| 6 | **`auto_sync_enabled`** | Instance 6، يجب أن يظل **False** | يفعّل Master Scheduler الذي يدفع المخزون والأسعار |
| 7 | **Master Scheduler** Cron (id=43) | يجب أن يظل **متوقفاً** | يُرسل عمليات كتابة خطيرة |
| 8 | **Full Sync** Cron (id=42) | يجب أن يظل **متوقفاً** | يشغّل جميع العمليات بما فيها الكتابة |
| 9 | **Export Stock** Cron (id=33) | يجب أن يظل **متوقفاً** | يدفع المخزون إلى Amazon |
| 10 | **Update Prices** Cron (id=29) | يجب أن يظل **متوقفاً** | يدفع الأسعار إلى Amazon |
| 11 | **`stock_push_interval`** | Instance 6، يجب أن يظل **disabled** | حماية ضد دفع المخزون |
| 12 | **`price_push_interval`** | Instance 6، يجب أن يظل **disabled** | حماية ضد دفع الأسعار |
| 13 | **حركات المخزون الافتتاحية** | Stock Picking/Move من 2026-09-15 | سجل تاريخي للمخزون الافتتاحي |
| 14 | **بيانات API** | Refresh Token, Client ID, Client Secret | يكسر كل اتصال Amazon |

---

## 21. قائمة مهام اليوم الأول لـ Implementer

### اليوم 1 — التعارف والاستكشاف

- [ ] اقرأ القسم 1 (نظرة عامة تنفيذية) — افهم ماذا يفعل التكامل
- [ ] سجّل الدخول إلى Odoo وتنقل في قائمة Amazon (القسم 2)
- [ ] افتح نموذج Amazon Instance (Amazon > Configuration > Instances)
- [ ] راجع الـ 19 ربط منتج (Amazon > Catalog > Products)
- [ ] تصفح مواقع FBA (Inventory > Configuration > Locations، فلتر "Amazon")
- [ ] تحقق من Order Pipeline الحي: افتح Amazon > Orders > Import Jobs — أكّد أن المهام الحديثة مكتملة
- [ ] تحقق من FBA Sale Stock Events (Amazon > FBA > FBA Sale Stock Events) — أكّد أن الكل "done"
- [ ] اقرأ القسم 7 (Cutover V2) — افهم لماذا يوجد وماذا يحمي
- [ ] اقرأ القسم 9 (الأتمتة المشغّلة حالياً) — اعرف أي Crons نشطة
- [ ] اقرأ القسم 10 (قواعد السلامة الحرجة) — اعرف ما لا يجب فعله
- [ ] اقرأ القسم 20 (قائمة الممنوعات)
- [ ] **لا تفعّل أي أتمتة إضافية في اليوم الأول**

### اليوم 2 — تحضير المحاسبة

- [ ] اجتمع مع المحاسب
- [ ] راجع القسم 14 (تسليم المحاسبة و Settlement)
- [ ] امشِ مع المحاسب عبر الـ 18 عنصر إعداد
- [ ] اتفقا على Chart of Accounts لـ Amazon
- [ ] اتفقا على Settlement Cutoff Date
- [ ] أكّدا Settlement Accounting Strategy
- [ ] ناقشا معاملة الضرائب المصرية
- [ ] **لا تستورد Settlements بعد** — فقط حضّر هيكل الحسابات

### اليوم 3+ — اختبار Settlement

- [ ] أعدّ جميع الحسابات في نموذج Amazon Instance
- [ ] اتبع الإجراء في القسم 15 (اختبار أول Settlement)
- [ ] استورد Settlement واحد
- [ ] أنشئ قيد محاسبي مسودة
- [ ] اطلب مراجعة المحاسب
- [ ] كرر حتى الصحة
- [ ] وثّق Account Mappings النهائية

### الأسبوع 2+ — تفعيل الخصائص

- [ ] بعد التحقق من محاسبة Settlement، فكّر في تفعيل Settlement Import Cron
- [ ] ناقش مع العميل: Returns, Adjustments, Reimbursements (القسم 16)
- [ ] ناقش مع العميل: سياسة المنتج/السعر (القسم 18)
- [ ] فعّل الخصائص واحدة تلو الأخرى مع المراقبة

---

## 22. مصفوفة حالة التشغيل

| الخاصية | الحالة | المسؤول | الإجراء التالي |
|---|---|---|---|
| Amazon API Connection | **يعمل** | المطور | مراقبة |
| Product SKU Mapping | **مكتمل** (19/19 مربوط) | Implementer | مراقبة لـ SKUs جديدة |
| FBA Opening Stock | **مكتمل** (FBAAUDIT/00007) | المطور | سجل تاريخي |
| Cutover V2 | **مفعّل** (15,269 Baselines) | المطور | لا تلمسه |
| Order Import (Cron 24) | **متوقف** — استُخدم عند بداية التشغيل، الآن متوقف | المطور | إعادة تفعيل عند الحاجة (آمن، قراءة فقط) |
| Order Status Sync (Cron 26) | **يعمل** (Cron كل 15 دقيقة) — الآلية الأساسية لاكتشاف الطلبات | آلي | مراقبة يومية |
| FBA Sale Stock Depletion | **يعمل** (Cron كل دقيقة) | آلي | مراقبة لـ manual_review |
| FBA Inventory Audit | **يعمل** (يومي + معالج كل 5 دقائق) | آلي | مراجعة أسبوعية |
| Inbound Shipments | **يعمل** (Crons نشطة) | العمليات | استخدام عند الشحن إلى Amazon |
| Connection Health | **يعمل** (مراقبة كل 15 دقيقة) | آلي | تحقق من التنبيهات |
| Operational Monitoring | **يعمل** (Dashboard، تنبيهات، مهام متعثرة) | آلي | تحقق يومي |
| Stock Push إلى Amazon | **متوقف** — غير مطلوب لـ FBA | المطور | لا تفعّله |
| Price Push إلى Amazon | **متوقف** — لا سياسة معتمدة | العميل + المطور | يتطلب قرار العميل |
| Product Sync Automation | **متوقف** | Implementer | يتطلب قرار سياسة |
| Settlement Import | **في الانتظار** — المحاسب يجب أن يُعدّ | المحاسب + Implementer | إعداد الحسابات أولاً |
| Settlement Accounting | **في الانتظار** — اليوميات/الحسابات غير مُعدّة | المحاسب | إعداد كامل مطلوب |
| Payout Reconciliation | **في الانتظار** — Bank Journal غير مُعدّ | المحاسب | بعد إعداد Settlement |
| Customer Returns Import | **يتطلب قرار** | العميل | الموافقة للتفعيل |
| Inventory Adjustments Import | **يتطلب قرار** | العميل | الموافقة للتفعيل |
| Reimbursements Import | **يتطلب قرار** | العميل | الموافقة للتفعيل |
| Removal Order Tracking | **يتطلب قرار** | العميل | الموافقة إذا يستخدم Removals |
| Egyptian Tax Configuration | **في الانتظار** | المحاسب + مستشار الضرائب | حرج للقيود الصحيحة |
| AI Features | **غير مهيأة** | اختياري | يتطلب API Key |

---

## 23. ملخص التسليم النهائي

### أ. ما يعمل بالفعل

FBA Order و Inventory Pipeline يعمل بالكامل:

- يتم اكتشاف طلبات Amazon وتحديثها عبر Order Status Sync كل 15 دقيقة.
- تنفيذ FBA يخصم تلقائياً من مخزون Sellable في Odoo.
- Cutover V2 يمنع الخصم المزدوج للطلبات التاريخية.
- عمليات Inventory Audit اليومية تقارن Odoo مع Amazon.
- سير عمل Inbound Shipment نشط لإرسال مخزون جديد إلى Amazon.
- صحة الاتصال والتنبيهات والمراقبة التشغيلية نشطة.
- جميع الـ 19 منتج مربوطة. جميع البيانات متطابقة.

### ب. ما يجب أن يكمله Implementer

1. **إعداد Settlement Accounting** — العمل مع المحاسب لـ:
   - اختيار أو إنشاء Settlement Journal
   - ربط جميع حسابات Amazon الـ 15+ (رسوم/إيرادات/مرتجعات)
   - تحديد Settlement Cutoff Date
   - إعداد معاملة الضرائب المصرية
   - إعداد Payout Bank Journal
   - الاختبار مع Settlement حقيقي واحد قبل تفعيل الأتمتة

2. **قرارات السياسة التجارية** — العمل مع العميل لتقرير:
   - سياسة مزامنة المنتجات (آلية أو يدوية)
   - سياسة إدارة الأسعار (Odoo المرجع أو Amazon المرجع)
   - تفعيل Returns, Adjustments, Reimbursements, و Removal Tracking

3. **المراقبة المستمرة** — إنشاء روتين فحص يومي وأسبوعي حسب القسم 12.

### ج. ما يجب ألا يتغيّر

| العنصر | السبب |
|---|---|
| Cutover V2 Run 3, Baselines, Cutover Timestamp | يمنع الخصم المزدوج للطلبات التاريخية |
| `auto_sync_enabled` (يجب أن يظل False) | يمنع Master Scheduler من إرسال Stock/Price Push |
| Master Scheduler, Full Sync, Export Stock, Update Prices Crons (يجب أن تظل متوقفة) | يمنع الكتابة إلى Amazon |
| `stock_push_interval`, `price_push_interval` (يجب أن تظل disabled) | حماية Defense-in-Depth |
| هيكل مواقع FBA (ids 29-37) | أساس كل تتبع المخزون |
| مرجع المخزون الافتتاحي (FBAAUDIT/00007) | مسار Audit التاريخي |
| بيانات API | يكسر كل الاتصال إذا تغيّر بشكل خاطئ |

---

## ابدأ من هنا — Implementer

إذا كنت تقرأ هذه الوثيقة لأول مرة، اتبع هذه القواعد:

1. **لا تُعد إعداد التكامل التقني.** بيانات API، إعدادات Marketplace، مواقع FBA، وحقول اتصال Instance مُعدّة بالفعل وتعمل. تغييرها سيكسر Pipeline الحي.

2. **لا تُعد تحميل المخزون الافتتاحي.** مخزون FBA الافتتاحي تم تحميله مرة واحدة في 2026-09-15 وهو أساس تاريخي دائم. لا تُعد التحميل أو الاستيراد أو "التحديث" أبداً.

3. **لا تعدّل Cutover V2.** Run 3 بـ 15,269 Baseline مفعّل ويحمي جميع الطلبات التاريخية من الخصم المزدوج. لا تُعد البناء أو تحذف أو تعدّل Baselines أو Timestamps.

4. **لا تفعّل Master Auto-Sync (Cron 43)، Full Sync (Cron 42)، Export Stock (Cron 33)، أو Update Prices (Cron 29).** هذه تكتب إلى Amazon ومتوقفة عمداً. علم `auto_sync_enabled` يجب أن يظل False. `stock_push_interval` و `price_push_interval` يجب أن يظلا `disabled`.

5. **اقضِ اليوم الأول في فهم سير عمل Order/FBA الحي.** اتبع قائمة مهام اليوم الأول في القسم 21. اقرأ جولة الموديول، تنقل في القوائم، تحقق أن الطلبات تتدفق، Events تُعالج، و Audits تكتمل.

6. **أول عمل تنفيذي متبقٍ هو إعداد Accounting & Settlement.** اعمل مع المحاسب لإعداد Settlement Journal، Clearing Account، حسابات الرسوم/الإيرادات، معاملة الضرائب، و Cutoff Date. راجع القسم 14.

7. **اتبع إجراء أول Settlement المتحكّم** (القسم 15) قبل تفعيل أتمتة Settlement. استورد Settlement واحد، أنشئ قيد مسودة، اطلب مراجعة المحاسب، كرر حتى الصحة، ثم فكّر في الأتمتة.

8. **عند الشك، اسأل المطور.** أي تغيير في Crons، Intervals، أعلام المزامنة، أو حالة Cutover يجب مناقشته مع المطور أولاً.

---

## قائمة تحقق قبول التسليم

أكمل هذه القائمة بعد أسبوعك الأول من التأهيل.

### الفهم (اليوم 1)

- [ ] قرأت الأقسام 1-11 من هذه الوثيقة
- [ ] أستطيع التنقل في قائمة Amazon في Odoo وأجد جميع الشاشات الرئيسية
- [ ] أفهم أي Crons نشطة وماذا تفعل
- [ ] أفهم قائمة الممنوعات ولماذا كل عنصر محمي
- [ ] أعرف الفرق بين مطابقة الخصم ومقارنة Audit
- [ ] تحققت أن الطلبات تُستورد و Events تُعالج

### المراقبة (اليوم 2-3)

- [ ] أجريت فحصاً يومياً حسب القسم 12
- [ ] راجعت آخر Inventory Audit
- [ ] أعرف أين أجد Sync Logs والتنبيهات التشغيلية
- [ ] أعرف متى أصعّد ومتى أنتظر

### تحضير المحاسبة (اليوم 3-5)

- [ ] راجعت القسم 14 مع المحاسب
- [ ] المحاسب أكّد Chart of Accounts لـ Amazon
- [ ] اتفقنا على Settlement Cutoff Date
- [ ] اتفقنا على Settlement Accounting Strategy

### اختبار أول Settlement (الأسبوع 2+)

- [ ] جميع Account Mappings مُعدّة في نموذج Instance
- [ ] تم استيراد Settlement واحد بنجاح
- [ ] تم إنشاء قيد محاسبي مسودة ومراجعته
- [ ] المحاسب وافق على هيكل القيد
- [ ] Payout Bank Journal مُعدّ

### التوقيع

| الدور | الاسم | التاريخ | التوقيع |
|---|---|---|---|
| Implementer | | | |
| المحاسب | | | |
| عمليات العميل | | | |
| المطور | | | |

---

*تم إعداد هذه الوثيقة في 2026-09-16 وتم تحديثها بمراجعة QA مقابل قاعدة بيانات الإنتاج `amazon_prod12sep` والكود المصدري `sdlc_amazon_connector`. تم التحقق من لقطة الإنتاج في 2026-09-16 17:15 UTC. لم يتم تعديل أي بيانات أو إعدادات إنتاج أثناء الإعداد.*
