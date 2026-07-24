import json
import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError

from .amazon_api import AmazonAPI
from ..services.ai_service import AmazonAIService

_logger = logging.getLogger(__name__)


class AmazonProduct(models.Model):
    _name = 'amazon.product'
    _description = 'Amazon Product'
    _rec_name = 'name'

    # ══════════════════════════════════════════════════
    # Tab 1: Product Identity
    # ══════════════════════════════════════════════════
    name = fields.Char('Item Name', required=True)
    product_type = fields.Char('Product Type', help="e.g. SHOES, SHIRT, HOME_BED_AND_BATH")
    product_type_group = fields.Selection([
        ('footwear', 'Footwear'),
        ('clothing', 'Clothing & Apparel'),
        ('electronics', 'Electronics & Computers'),
        ('beauty', 'Beauty & Personal Care'),
        ('home', 'Home & Kitchen'),
        ('sports', 'Sports & Outdoors'),
        ('food', 'Grocery & Food'),
        ('books', 'Books & Media'),
        ('toys', 'Toys & Games'),
        ('jewelry', 'Jewelry & Watches'),
        ('automotive', 'Automotive'),
        ('general', 'General / Other'),
    ], string='Product Category', compute='_compute_product_type_group', store=True, readonly=False,
       help="Auto-detected from Product Type. Controls which fields appear in Product Details tab.")
    browse_node = fields.Char('Recommended Browse Node', help="Category path e.g. Fashion > Men > Shoes")
    brand = fields.Char('Brand Name')
    barcode = fields.Char('External Product ID', help="EAN, UPC, ISBN or GTIN")
    barcode_type = fields.Selection([
        ('EAN', 'EAN'),
        ('UPC', 'UPC'),
        ('GTIN', 'GTIN'),
        ('ISBN', 'ISBN'),
    ], string='Product ID Type', default='EAN')
    no_barcode = fields.Boolean('No Product ID (GTIN Exemption)')
    asin = fields.Char('ASIN')
    sku = fields.Char(
        'Amazon SKU',
        help="Amazon seller SKU. For automatic mapping, this must match the Odoo product Internal Reference.",
    )

    # Variations
    has_variations = fields.Boolean('This product has variations')
    var_team_name = fields.Boolean('Team Name')
    var_athlete = fields.Boolean('Athlete')
    var_color = fields.Boolean('Color')
    var_number_of_items = fields.Boolean('Number of Items')
    var_footwear_size = fields.Boolean('Footwear Size')
    var_size = fields.Boolean('Size')
    var_material = fields.Boolean('Material')
    var_pattern = fields.Boolean('Pattern')
    var_style = fields.Boolean('Style')
    variation_theme = fields.Char('Variation Theme', compute='_compute_variation_theme', store=True, readonly=False)
    is_parent = fields.Boolean('Is Parent Listing', default=False)
    parent_asin = fields.Char('Parent ASIN')
    parent_product_id = fields.Many2one('amazon.product', string='Parent Product', ondelete='set null')
    child_ids = fields.One2many('amazon.product', 'parent_product_id', string='Variations')

    PRODUCT_TYPE_MAP = {
        'footwear': [
            'SHOES', 'SANDAL', 'BOOT', 'SLIPPER', 'SNEAKER', 'HEEL', 'FLIP_FLOP',
            'LOAFER', 'MOCCASIN', 'CLOG', 'OXFORD', 'FLAT', 'PUMP', 'WEDGE',
            'FOOTWEAR', 'SHOE',
        ],
        'clothing': [
            'SHIRT', 'DRESS', 'PANTS', 'JEANS', 'JACKET', 'COAT', 'SWEATER',
            'HOODIE', 'BLAZER', 'SUIT', 'SKIRT', 'SHORTS', 'LEGGING', 'UNDERWEAR',
            'SOCK', 'TIE', 'SCARF', 'GLOVE', 'HAT', 'CAP', 'BELT', 'SWIMWEAR',
            'SLEEPWEAR', 'LINGERIE', 'SAREE', 'KURTA', 'DUPATTA', 'SALWAR',
            'CLOTHING', 'APPAREL', 'TSHIRT', 'T_SHIRT', 'TROUSER', 'DENIM',
        ],
        'electronics': [
            'LAPTOP', 'PHONE', 'TABLET', 'COMPUTER', 'MONITOR', 'KEYBOARD',
            'MOUSE', 'HEADPHONE', 'SPEAKER', 'CAMERA', 'TELEVISION', 'TV',
            'CHARGER', 'CABLE', 'ADAPTER', 'BATTERY', 'PRINTER', 'SCANNER',
            'ELECTRONIC', 'POWERBANK', 'EARPHONE', 'SMARTWATCH', 'CONSOLE',
        ],
        'beauty': [
            'BEAUTY', 'COSMETIC', 'MAKEUP', 'SKINCARE', 'HAIRCARE', 'PERFUME',
            'FRAGRANCE', 'LOTION', 'CREAM', 'SHAMPOO', 'CONDITIONER', 'SERUM',
            'LIPSTICK', 'FOUNDATION', 'NAIL', 'DEODORANT', 'SUNSCREEN',
        ],
        'home': [
            'HOME', 'KITCHEN', 'FURNITURE', 'BEDDING', 'CURTAIN', 'RUG',
            'LAMP', 'CANDLE', 'VASE', 'FRAME', 'STORAGE', 'ORGANIZER',
            'COOKWARE', 'BAKEWARE', 'UTENSIL', 'APPLIANCE', 'TOWEL',
            'HOME_BED_AND_BATH', 'PILLOW', 'MATTRESS', 'BLANKET',
        ],
        'sports': [
            'SPORT', 'FITNESS', 'GYM', 'YOGA', 'BICYCLE', 'CAMPING',
            'HIKING', 'SWIMMING', 'CRICKET', 'FOOTBALL', 'BASKETBALL',
            'TENNIS', 'BADMINTON', 'SPORTING_GOODS', 'OUTDOOR', 'EXERCISE',
        ],
        'food': [
            'GROCERY', 'FOOD', 'SNACK', 'BEVERAGE', 'DRINK', 'SPICE',
            'SUPPLEMENT', 'VITAMIN', 'PROTEIN', 'TEA', 'COFFEE', 'CEREAL',
        ],
        'books': [
            'BOOK', 'EBOOK', 'MAGAZINE', 'COMIC', 'NOTEBOOK', 'DIARY',
            'STATIONERY', 'ART_SUPPLY', 'MEDIA', 'DVD', 'CD', 'VINYL',
        ],
        'toys': [
            'TOY', 'GAME', 'PUZZLE', 'BOARD_GAME', 'DOLL', 'ACTION_FIGURE',
            'LEGO', 'BUILDING_BLOCK', 'STUFFED_ANIMAL', 'BABY_PRODUCT',
        ],
        'jewelry': [
            'JEWELRY', 'WATCH', 'RING', 'NECKLACE', 'BRACELET', 'EARRING',
            'PENDANT', 'CHAIN', 'BANGLE', 'ANKLET', 'BROOCH', 'CUFFLINK',
        ],
        'automotive': [
            'AUTOMOTIVE', 'CAR', 'BIKE', 'MOTORCYCLE', 'TIRE', 'OIL',
            'ACCESSORY_AUTO', 'HELMET', 'TOOL_AUTO',
        ],
    }

    @api.depends('product_type')
    def _compute_product_type_group(self):
        for rec in self:
            ptype = (rec.product_type or '').upper().replace('-', '_').replace(' ', '_')
            found = 'general'
            if ptype:
                for group, keywords in self.PRODUCT_TYPE_MAP.items():
                    for kw in keywords:
                        if kw in ptype or ptype in kw:
                            found = group
                            break
                    if found != 'general':
                        break
            rec.product_type_group = found

    @api.depends('var_team_name', 'var_athlete', 'var_color', 'var_number_of_items',
                 'var_footwear_size', 'var_size', 'var_material', 'var_pattern', 'var_style')
    def _compute_variation_theme(self):
        mapping = [
            ('var_team_name', 'TeamName'),
            ('var_athlete', 'Athlete'),
            ('var_color', 'Color'),
            ('var_number_of_items', 'NumberOfItems'),
            ('var_footwear_size', 'FootwearSize'),
            ('var_size', 'Size'),
            ('var_material', 'Material'),
            ('var_pattern', 'Pattern'),
            ('var_style', 'Style'),
        ]
        for rec in self:
            parts = [theme for field, theme in mapping if rec[field]]
            rec.variation_theme = '/'.join(parts) if parts else ''

    # ══════════════════════════════════════════════════
    # Tab 2: Description
    # ══════════════════════════════════════════════════
    description = fields.Text('Product Description')
    bullet_point_ids = fields.One2many('amazon.product.bullet.point', 'product_id', string='Bullet Points')
    search_terms = fields.Char('Search Terms', help="Backend keywords for Amazon search (max 250 chars)")

    # Images (9 slots like Amazon Seller Central)
    image_ids = fields.One2many('amazon.product.image', 'product_id', string='Product Images')
    image_url = fields.Char('Main Image URL')
    product_image = fields.Binary('Product Image', attachment=True)

    def action_download_image(self):
        """Download main image from URL and store as binary."""
        import base64
        import requests as req
        self.ensure_one()
        url = self.image_url
        if not url:
            raise UserError("No image URL to download.")
        try:
            resp = req.get(url, timeout=15)
            resp.raise_for_status()
            if resp.content:
                self.product_image = base64.b64encode(resp.content)
        except Exception as exc:
            raise UserError("Failed to download image: %s" % exc) from exc
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Image", "message": "Image downloaded.", "type": "success", "sticky": False},
        }

    # ══════════════════════════════════════════════════
    # Tab 3: Product Details
    # ══════════════════════════════════════════════════

    # -- Basic Info --
    has_customisations = fields.Selection([('Yes', 'Yes'), ('No', 'No')], string='Customisations', default='No')
    model_number = fields.Char('Model Number', help="e.g. RX2ER23")
    model_name = fields.Char('Model Name', help="e.g. Lunar Tempo")
    manufacturer = fields.Char('Manufacturer', help="e.g. Frye, Cole Haan, Puma")
    special_features = fields.Char('Special Features', help="Comma-separated: Flame-Resistant, Metatarsal Guard")
    lifestyle = fields.Char('Lifestyle', help="e.g. Fashion Casual, Themed")
    style = fields.Char('Style', help="e.g. Ballet, Clogs")
    target_gender = fields.Selection([
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Unisex', 'Unisex'),
    ], string='Target Gender')
    age_range = fields.Char('Age Range Description', help="e.g. Adult")
    lining_description = fields.Char('Lining Description', help="e.g. Fabric, Faux Shearling or Fur")
    pattern = fields.Char('Pattern', help="e.g. Checkered")
    item_type_name = fields.Char('Item Type Name', help="e.g. Ballet Flat, Boat Shoe")
    water_resistance_level = fields.Selection([
        ('not_water_resistant', 'Not Water Resistant'),
        ('water_resistant', 'Water Resistant'),
        ('waterproof', 'Waterproof'),
    ], string='Water Resistance Level')
    subject_character = fields.Char('Subject Character', help="e.g. Batman")
    color_map = fields.Char('Color Map', help="e.g. Red")
    color = fields.Char('Color', help="e.g. Cranberry")

    # -- Footwear Size --
    footwear_size_system = fields.Selection([
        ('US', 'US'),
        ('UK', 'UK'),
        ('EU', 'EU'),
        ('JP', 'JP'),
        ('IND', 'India (IND)'),
    ], string='Footwear Size System')
    footwear_age_group = fields.Char('Footwear Age Group', help="e.g. Adult, Big Kid")
    footwear_gender = fields.Selection([
        ('Men', 'Men'),
        ('Women', 'Women'),
        ('Boy', 'Boy'),
        ('Girl', 'Girl'),
        ('Unisex', 'Unisex'),
    ], string='Footwear Gender')
    footwear_size_class = fields.Selection([
        ('Numeric', 'Numeric'),
        ('Alpha', 'Alpha'),
    ], string='Footwear Size Class')
    footwear_width = fields.Char('Footwear Width', help="e.g. Medium, Narrow, Wide")
    footwear_size = fields.Char('Footwear Size', help="e.g. 8, 8.5, 9, Medium")

    # -- Category-specific attributes --
    occasion = fields.Char('Occasion', help="e.g. Funeral, Casual, Formal")
    theme = fields.Char('Theme', help="e.g. Sports")
    sole_material = fields.Char('Sole Material', help="e.g. Leather and Rubber")
    toe_style = fields.Char('Toe Style', help="e.g. Aluminum Toe, Bicycle Toe")
    manufacturer_contact = fields.Text('Manufacturer Contact Information')
    height_map = fields.Char('Height Map', help="e.g. High Top, Low Top, Mid Top")

    # -- Unit Count --
    unit_count = fields.Float('Unit Count', help="e.g. 72.0")
    unit_count_type = fields.Char('Unit Count Type', help="e.g. Count, Gram, Ounce")
    specific_uses = fields.Char('Specific Uses for Product', help="e.g. Dry Sanding")
    sport_type = fields.Char('Sport Type', help="e.g. Snowboarding")
    embellishment_feature = fields.Char('Embellishment Feature', help="e.g. Buckle")

    # -- External Product Information --
    external_info_entity = fields.Char('External Product Info Entity', help="e.g. HSN Code")
    external_info_value = fields.Char('External Product Information', help="e.g. QU85, 610510, 61051010")

    # -- Heel --
    heel_height = fields.Float('Heel Height')
    heel_height_unit = fields.Selection([
        ('inches', 'Inches'),
        ('centimeters', 'Centimeters'),
        ('millimeters', 'Millimeters'),
    ], string='Heel Height Unit', default='inches')
    heel_type = fields.Char('Heel Type', help="e.g. Block Heel, Chunky Heel")

    # -- Additional --
    seasons = fields.Char('Seasons', help="e.g. Fall, Spring")
    importer_contact = fields.Text('Importer Contact Information')
    packer_contact = fields.Text('Packer Contact Information')
    closure_type = fields.Char('Closure Type', help="e.g. Buckle, Slip-On")
    shoe_type = fields.Selection([
        ('athletic_shoe', 'Athletic Shoe'),
        ('boot', 'Boot'),
        ('casual_shoe', 'Casual Shoe'),
        ('dress_shoe', 'Dress Shoe'),
        ('sandal', 'Sandal'),
        ('slipper', 'Slipper'),
        ('work_shoe', 'Work Shoe'),
    ], string='Shoe Type')

    # -- Clothing-specific (SHIRT, DRESS, etc.) --
    fit_type = fields.Char('Fit Type', help="e.g. Regular Fit, Slim Fit, Loose Fit")
    department = fields.Char('Department', help="e.g. Men, Women, Boys, Girls, Unisex")
    number_of_items = fields.Integer('Number of Items', default=1)
    shirt_size = fields.Char('Shirt Size', help="e.g. S, M, L, XL, 40, 42")
    care_instructions = fields.Char('Care Instructions', help="e.g. Machine Wash, Hand Wash Only")
    special_size_type = fields.Char('Special Size Type', help="e.g. Big & Tall, Petite, Plus Size")
    sleeve_type = fields.Char('Sleeve Type', help="e.g. Half Sleeve, Full Sleeve, Sleeveless, Short Sleeve")
    neck_style = fields.Char('Neck Style', help="e.g. Round Neck, V-Neck, Collar, Mandarin Collar")

    # -- Dimensions & Weight --
    size = fields.Char('Size')
    material = fields.Char('Material')
    item_weight = fields.Float('Item Weight (kg)')
    item_length = fields.Float('Length (cm)')
    item_width = fields.Float('Width (cm)')
    item_height = fields.Float('Height (cm)')
    package_weight = fields.Float('Package Weight (kg)')
    package_length = fields.Float('Package Length (cm)')
    package_width = fields.Float('Package Width (cm)')
    package_height = fields.Float('Package Height (cm)')
    country_of_origin = fields.Char('Country of Origin')
    part_number = fields.Char('Manufacturer Part Number')

    # ══════════════════════════════════════════════════
    # Tab 4: Offer
    # ══════════════════════════════════════════════════
    skip_offer = fields.Boolean('Let me skip the offer data and add it later', default=False)

    # SKU & Quantity
    amazon_price = fields.Float('Amazon Price', help="Price imported from Amazon. Odoo remains the master after setup.")
    amazon_qty = fields.Float('Amazon Quantity', help="Quantity imported from Amazon for reference/reconciliation only.")
    sale_price = fields.Float('Sale Price')
    sale_start_date = fields.Date('Sale Start Date')
    sale_end_date = fields.Date('Sale End Date')
    mrp = fields.Float('MRP / List Price')

    # Condition
    condition_type = fields.Selection([
        ('new_new', 'New'),
        ('new_open_box', 'Open Box'),
        ('new_oem', 'OEM'),
        ('refurbished_refurbished', 'Refurbished'),
        ('used_like_new', 'Used - Like New'),
        ('used_very_good', 'Used - Very Good'),
        ('used_good', 'Used - Good'),
        ('used_acceptable', 'Used - Acceptable'),
    ], string='Condition', default='new_new')
    condition_note = fields.Text('Condition Note', help="Required for used/refurbished items")

    # Fulfillment
    fulfillment_channel = fields.Selection([
        ('MFN', 'I will ship this item myself (Self Ship) or I will pack this item and Amazon will pick up & ship (Easy Ship) — Merchant Fulfilled'),
        ('AFN', 'I want to use Fulfilled by Amazon (FBA) to ship my items and provide customer service if they sell — Fulfilled by Amazon'),
    ], string='Fulfillment Channel', default='MFN')

    handling_time = fields.Integer('Handling Time (days)', default=2, help="Business days to ship")
    max_order_qty = fields.Integer('Max Order Quantity')
    tax_code = fields.Char('Product Tax Code')

    # Item Dimensions (with per-field unit selectors)
    offer_item_length = fields.Float('Item Length')
    offer_item_length_unit = fields.Selection([
        ('centimeters', 'Centimetres'),
        ('inches', 'Inches'),
        ('meters', 'Metres'),
        ('millimeters', 'Millimetres'),
        ('feet', 'Feet'),
    ], string='Item Length Unit', default='centimeters')
    offer_item_width = fields.Float('Item Width')
    offer_item_width_unit = fields.Selection([
        ('centimeters', 'Centimetres'),
        ('inches', 'Inches'),
        ('meters', 'Metres'),
        ('millimeters', 'Millimetres'),
        ('feet', 'Feet'),
    ], string='Item Width Unit', default='centimeters')
    offer_item_height = fields.Float('Item Height')
    offer_item_height_unit = fields.Selection([
        ('centimeters', 'Centimetres'),
        ('inches', 'Inches'),
        ('meters', 'Metres'),
        ('millimeters', 'Millimetres'),
        ('feet', 'Feet'),
    ], string='Item Height Unit', default='centimeters')

    # ══════════════════════════════════════════════════
    # Tab 5: Safety & Compliance
    # ══════════════════════════════════════════════════

    # Country/Region of Origin
    compliance_country_of_origin = fields.Char('Country/Region of Origin', help="e.g. China, India, US")

    # Item Weight
    compliance_item_weight = fields.Float('Item Weight')
    compliance_item_weight_unit = fields.Selection([
        ('grams', 'Grams'),
        ('kilograms', 'Kilograms'),
        ('pounds', 'Pounds'),
        ('ounces', 'Ounces'),
    ], string='Item Weight Unit', default='grams')

    # Outer Material
    outer_material = fields.Char('Outer Material', help="e.g. Canvas, Corduroy")

    # Safety & Hazmat
    hazmat = fields.Boolean('Contains Hazardous Materials')
    hazmat_info = fields.Text('Hazmat Information')
    safety_warning = fields.Text('Safety Warning')
    cpsia_warning = fields.Selection([
        ('no_warning', 'No Warning Applicable'),
        ('choking_hazard_small_parts', 'Choking Hazard - Small Parts'),
        ('choking_hazard_balloon', 'Choking Hazard - Balloon'),
        ('choking_hazard_marble', 'Choking Hazard - Contains a Marble'),
        ('choking_hazard_is_small_ball', 'Choking Hazard - Is a Small Ball'),
    ], string='CPSIA Warning')
    battery_type = fields.Selection([
        ('none', 'No Battery'),
        ('alkaline', 'Alkaline'),
        ('lithium_ion', 'Lithium Ion'),
        ('lithium_metal', 'Lithium Metal'),
        ('nimh', 'NiMH'),
    ], string='Battery Type', default='none')
    is_expirable = fields.Boolean('Product is Expirable')

    # ══════════════════════════════════════════════════
    # Odoo Mapping & Sync
    # ══════════════════════════════════════════════════
    instance_id = fields.Many2one('amazon.instance', string='Amazon Instance', required=True, ondelete='cascade')
    odoo_product_id = fields.Many2one(
        'product.product',
        string='Odoo Product',
        help="ERP product linked to this Amazon marketplace record. Amazon SKU should match the product Internal Reference.",
    )
    last_sync_date = fields.Datetime('Last Synced')
    status = fields.Selection([
        ('Active', 'Active'),
        ('Inactive', 'Inactive'),
        ('Incomplete', 'Incomplete'),
    ], string='Status')
    odoo_stock = fields.Float('Odoo Stock', compute='_compute_odoo_fields')
    odoo_price = fields.Float('Odoo Price', compute='_compute_odoo_fields')

    _sql_constraints = [
        ('unique_sku_instance', 'unique(sku, instance_id)', 'SKU must be unique per Amazon instance.'),
    ]

    def _compute_odoo_fields(self):
        for rec in self:
            rec.odoo_stock = rec.odoo_product_id.qty_available if rec.odoo_product_id else 0
            rec.odoo_price = rec.odoo_product_id.list_price if rec.odoo_product_id else 0

    # ══════════════════════════════════════════════════
    # Build Amazon Attributes Dict (shared by create & update)
    # ══════════════════════════════════════════════════

    def _build_amazon_attributes(self):
        """Build the attributes dict for Amazon Listings API from all tabs."""
        self.ensure_one()
        instance = self.instance_id
        mp = instance.marketplace_id
        currency = instance._get_currency_code()

        # Use Amazon fields primarily, Odoo as fallback
        odoo = self.odoo_product_id
        name = self.name or (odoo.name if odoo else '')
        # ALWAYS use amazon_price for Amazon — never Odoo list_price
        price = self.amazon_price or (odoo.list_price if odoo else 0)
        desc = self.description or (odoo.description_sale if odoo else '') or ''
        barcode = self.barcode or (odoo.barcode if odoo else '')

        attrs = {}

        # ── Product Identity ──
        attrs["condition_type"] = [{"value": self.condition_type or "new_new"}]
        attrs["item_name"] = [{"value": name, "marketplace_id": mp}]

        # Auto-generate barcode if missing
        if not barcode and not self.no_barcode:
            self.action_generate_barcode()
            barcode = self.barcode or ''

        if barcode and not self.no_barcode:
            attrs["externally_assigned_product_identifier"] = [{
                "type": self.barcode_type or "EAN",
                "value": barcode,
                "marketplace_id": mp,
            }]

        if self.brand:
            attrs["brand"] = [{"value": self.brand}]

        if self.asin:
            attrs["merchant_suggested_asin"] = [{"value": self.asin, "marketplace_id": mp}]

        # ── Variations ──
        if self.has_variations and self.variation_theme:
            attrs["variation_theme"] = [{"name": self.variation_theme}]
        if self.parent_asin:
            attrs["child_parent_sku_relationship"] = [{
                "child_relationship_type": "variation",
                "parent_sku": self.parent_asin,
            }]

        # ── Description ──
        if desc:
            attrs["product_description"] = [{"value": desc, "marketplace_id": mp}]

        bullet_points = []
        for bp in self.bullet_point_ids.sorted('sequence'):
            if bp.name:
                bullet_points.append({"value": bp.name, "marketplace_id": mp})
        if bullet_points:
            attrs["bullet_point"] = bullet_points

        if self.search_terms:
            attrs["generic_keyword"] = [{"value": self.search_terms, "marketplace_id": mp}]

        # Images
        sorted_images = self.image_ids.sorted('sequence')
        if sorted_images:
            main = sorted_images[0]
            url = main.image_url
            if url:
                attrs["main_product_image_locator"] = [{"media_location": url, "marketplace_id": mp}]
            other_urls = [img.image_url for img in sorted_images[1:] if img.image_url]
            for i, url in enumerate(other_urls[:8]):
                attrs["other_product_image_locator_%d" % (i + 1)] = [{"media_location": url, "marketplace_id": mp}]
        elif self.image_url:
            attrs["main_product_image_locator"] = [{"media_location": self.image_url, "marketplace_id": mp}]

        # ── Product Details ──

        # Helper for simple text attrs
        def _set(key, val, with_mp=False):
            if val:
                item = {"value": val}
                if with_mp:
                    item["marketplace_id"] = mp
                attrs[key] = [item]

        # Helper for comma-separated multi-value attrs
        def _set_multi(key, val, with_mp=False):
            if val:
                items = []
                for v in str(val).split(','):
                    v = v.strip()
                    if v:
                        item = {"value": v}
                        if with_mp:
                            item["marketplace_id"] = mp
                        items.append(item)
                if items:
                    attrs[key] = items

        ptype_upper = (self.product_type or '').upper()
        is_footwear = self.product_type_group == 'footwear' or any(
            kw in ptype_upper for kw in [
                'SHOE', 'SANDAL', 'BOOT', 'HEEL', 'FLAT', 'SNEAKER', 'SLIPPER',
                'FOOTWEAR', 'ATHLETIC', 'LOAFER', 'MOCCASIN', 'CLOG', 'OXFORD',
            ]
        )
        _logger.info("=== BUILD ATTRS: type=%s, group=%s, is_footwear=%s ===",
                      self.product_type, self.product_type_group, is_footwear)

        # ── Basic Info (common to all types) ──
        _set("manufacturer", self.manufacturer)
        _set("model_number", self.model_number or self.sku)
        _set("model_name", self.model_name)
        _set("part_number", self.part_number)
        _set_multi("special_feature", self.special_features, True)
        _set("item_type_name", self.item_type_name, True)
        _set("country_of_origin", self.country_of_origin)
        _set("color", self.color, True)
        _set("material", self.material, True)
        _set("pattern", self.pattern, True)
        _set("lining_description", self.lining_description, True)
        _set("age_range_description", self.age_range)
        _set("subject_character", self.subject_character, True)
        _set_multi("theme", self.theme, True)
        _set_multi("seasons", self.seasons, True)

        # These attributes are REJECTED by some types (SHIRT, SHOES) — only send for general
        is_clothing = self.product_type_group == 'clothing' or any(
            kw in ptype_upper for kw in ['SHIRT', 'DRESS', 'PANT', 'JEAN', 'JACKET', 'KURTA']
        )
        if not is_footwear and not is_clothing:
            _set_multi("occasion_type", self.occasion, True)
            _set_multi("specific_uses_for_product", self.specific_uses, True)
            _set_multi("sport_type", self.sport_type, True)
        _set_multi("embellishment_feature", self.embellishment_feature, True)

        # target_gender — Amazon India wants lowercase: "male", "female", "unisex"
        gender = self.target_gender
        if gender:
            attrs["target_gender"] = [{"value": gender.lower()}]

        # ── FOOTWEAR-SPECIFIC (SHOES, SANDAL, BOOT, etc.) ──
        if is_footwear:
            # outer — attribute name is "outer" (NOT "outer_material")
            outer_mat = self.outer_material or self.material or 'Synthetic'
            attrs["outer"] = []
            for v in str(outer_mat).split(','):
                v = v.strip()
                if v:
                    attrs["outer"].append({"value": v, "marketplace_id": mp})
            if not attrs["outer"]:
                attrs["outer"] = [{"value": "Synthetic", "marketplace_id": mp}]

            # style — REQUIRED for SHOES
            _set("style", self.style or self.item_type_name or 'Casual', True)

            # height_map — REQUIRED for SHOES
            _set("height_map", self.height_map or 'Ankle', True)

            # rtip_manufacturer_contact_information (India SHOES specific)
            _set("rtip_manufacturer_contact_information", self.manufacturer_contact or "Contact seller for details")

            # ── footwear_size ──
            # Amazon India (A21TJRUUN4KGV) valid combos — discovered from API errors:
            #   size_system: "IND" (India), age_group depends on marketplace
            # For other marketplaces: EU, US, UK
            fs_val = self.footwear_size or self.size or '7'

            # Detect India marketplace
            is_india = mp == 'A21TJRUUN4KGV'
            if is_india:
                fs_system = 'IND'
                fs_age = 'adult'
                gender_lower = (gender or 'male').lower()
                if gender_lower in ('female', 'women'):
                    fs_class = 'womens_shoes'
                else:
                    fs_class = 'mens_shoes'
            else:
                fs_system = self.footwear_size_system or 'EU'
                fs_age = self.footwear_age_group or 'adult'
                gender_lower = (gender or 'male').lower()
                if gender_lower in ('female', 'women'):
                    fs_class = 'womens_shoes'
                else:
                    fs_class = 'mens_shoes'

            attrs["footwear_size"] = [{
                "size": str(fs_val),
                "size_system": fs_system,
                "size_class": fs_class,
                "age_group": fs_age,
                "marketplace_id": mp,
            }]
            _logger.info("footwear_size: %s", attrs["footwear_size"])

            # sole_material
            _set("sole_material", self.sole_material or 'Rubber', True)
            _set("toe_style", self.toe_style, True)

            # heel — REQUIRED (attribute name is "heel")
            heel_val = self.heel_type or 'No Heel'
            attrs["heel"] = [{"value": heel_val, "marketplace_id": mp}]

            # closure — REQUIRED (attribute name is "closure")
            closure_val = self.closure_type or 'Pull On'
            attrs["closure"] = [{"value": closure_val, "marketplace_id": mp}]

            # water_resistance_level
            attrs["water_resistance_level"] = [{"value": self.water_resistance_level or "Not Water Resistant"}]

            _set("shoe_type", self.shoe_type, True)

        else:
            # ── NON-FOOTWEAR ──
            is_clothing = self.product_type_group == 'clothing' or any(
                kw in ptype_upper for kw in ['SHIRT', 'DRESS', 'PANT', 'JEAN', 'JACKET', 'KURTA', 'SAREE', 'SUIT']
            )

            if is_clothing:
                # CLOTHING-specific attributes (SHIRT, DRESS, etc.)
                _set("fit_type", self.fit_type or 'Regular Fit', True)

                # department — Amazon India uses lowercase
                dept = self.department or self.target_gender or 'Men'
                _set("department", dept, True)

                attrs["number_of_items"] = [{"value": self.number_of_items or 1}]

                # shirt_size — India uses alphanumeric sizes directly
                ss_val = self.shirt_size or self.size or 'M'
                attrs["shirt_size"] = [{"value": ss_val, "marketplace_id": mp}]

                _set("care_instructions", self.care_instructions or 'Machine Wash', True)
                _set("special_size_type", self.special_size_type or 'Standard', True)
                _set("style", self.style or 'Casual', True)
                _set("pattern", self.pattern, True)
                _set("fabric_type", self.material or 'Cotton', True)

                # sleeve — REQUIRED (send as BOTH "sleeve" and "sleeve_type" to cover all cases)
                sleeve_val = self.sleeve_type or 'Half Sleeve'
                attrs["sleeve"] = [{"value": sleeve_val, "marketplace_id": mp}]

                # rtip_manufacturer_contact_information — India specific
                _set("rtip_manufacturer_contact_information", self.manufacturer_contact or "Contact seller for details")

                # Closure for clothing
                if self.closure_type:
                    _set("closure", self.closure_type, True)
            else:
                # GENERAL (Electronics, Home, etc.)
                _set("lifestyle", self.lifestyle, True)
                _set("style", self.style, True)
                _set("size", self.size, True)
                if self.closure_type:
                    _set_multi("closure_type", self.closure_type, True)

            # Common non-footwear — use rtip_ for India, regular for others
            if mp == 'A21TJRUUN4KGV':
                _set("rtip_manufacturer_contact_information", self.manufacturer_contact or "Contact seller for details")
            else:
                _set("manufacturer_contact_information", self.manufacturer_contact)

        # ── Unit Count — value must be string ──
        uc_val = str(int(self.unit_count or 1))
        uc_type = self.unit_count_type or ("Pair" if is_footwear else "Unit")
        attrs["unit_count"] = [{"value": uc_val, "type": uc_type}]

        # ── External Product Info / HSN ──
        if self.external_info_entity and self.external_info_value:
            attrs["external_product_information"] = [{
                "entity": self.external_info_entity,
                "value": self.external_info_value,
            }]
        else:
            hsn = "64041100" if is_footwear else "62000000"
            attrs["external_product_information"] = [{"entity": "HSN", "value": hsn}]

        # ── Importer / Packer ──
        _set("importer_contact_information", self.importer_contact or "Contact seller for details")
        _set("packer_contact_information", self.packer_contact or "Contact seller for details")

        # ── Dimensions & Weight ──
        if self.item_weight:
            attrs["item_weight"] = [{"value": self.item_weight, "unit": "kilograms"}]
        dim = {}
        if self.item_length:
            dim["length"] = {"value": self.item_length, "unit": "centimeters"}
        if self.item_width:
            dim["width"] = {"value": self.item_width, "unit": "centimeters"}
        if self.item_height:
            dim["height"] = {"value": self.item_height, "unit": "centimeters"}
        if dim:
            for d in ("length", "width", "height"):
                if d not in dim:
                    dim[d] = {"value": 10.0, "unit": "centimeters"}
            attrs["item_dimensions"] = [dim]
        elif is_footwear:
            attrs["item_dimensions"] = [{"length": {"value": 30, "unit": "centimeters"}, "width": {"value": 20, "unit": "centimeters"}, "height": {"value": 12, "unit": "centimeters"}}]

        if self.package_weight:
            attrs["item_package_weight"] = [{"value": self.package_weight, "unit": "kilograms"}]
        if self.package_length and self.package_width and self.package_height:
            attrs["item_package_dimensions"] = [{
                "length": {"value": self.package_length, "unit": "centimeters"},
                "width": {"value": self.package_width, "unit": "centimeters"},
                "height": {"value": self.package_height, "unit": "centimeters"},
            }]

        # ── Offer (Amazon India Listings API format) ──
        # Key rule: our_price ≤ MRP. Amazon checks: all prices ≤ MRP.
        if not self.skip_offer:
            if price:
                # MRP must be ≥ selling price
                mrp_val = self.mrp if self.mrp and self.mrp >= price else price
                # Our selling price (must be ≤ MRP)
                selling = min(price, mrp_val)

                offer = {
                    "marketplace_id": mp,
                    "currency": currency,
                    "our_price": [{"schedule": [{"value_with_tax": selling}]}],
                    "maximum_retail_price": [{"schedule": [{"value_with_tax": mrp_val}]}],
                    "minimum_seller_allowed_price": [{"schedule": [{"value_with_tax": selling}]}],
                }
                attrs["purchasable_offer"] = [offer]

                # list_price needs value_with_tax (not "value")
                attrs["list_price"] = [{"value_with_tax": mrp_val, "currency": currency}]

            if self.sale_price and self.sale_start_date:
                attrs["sale_from_date"] = [{"value": str(self.sale_start_date)}]
            if self.sale_price and self.sale_end_date:
                attrs["sale_end_date"] = [{"value": str(self.sale_end_date)}]

            fc_code = "DEFAULT" if self.fulfillment_channel == 'MFN' else "AMAZON_NA"
            fulfillment_data = {
                "fulfillment_channel_code": fc_code,
                "quantity": int(self.amazon_qty or 0),
            }
            if self.handling_time and self.fulfillment_channel == 'MFN':
                fulfillment_data["lead_time_to_ship_max_days"] = self.handling_time
            attrs["fulfillment_availability"] = [fulfillment_data]

            if self.max_order_qty:
                attrs["max_order_quantity"] = [{"value": self.max_order_qty}]
            if self.tax_code:
                attrs["product_tax_code"] = [{"value": self.tax_code}]
            if self.condition_note:
                attrs["condition_note"] = [{"value": self.condition_note, "marketplace_id": mp}]

        # ── Item Dimensions (Offer section) ──
        if self.offer_item_length:
            attrs["item_length"] = [{"value": self.offer_item_length, "unit": self.offer_item_length_unit or "centimeters"}]
        if self.offer_item_width:
            attrs["item_width"] = [{"value": self.offer_item_width, "unit": self.offer_item_width_unit or "centimeters"}]
        if self.offer_item_height:
            attrs["item_height"] = [{"value": self.offer_item_height, "unit": self.offer_item_height_unit or "centimeters"}]

        # ── Safety & Compliance ──
        if self.compliance_country_of_origin:
            attrs["country_of_origin"] = [{"value": self.compliance_country_of_origin}]
        if self.compliance_item_weight:
            attrs["item_weight"] = [{
                "value": self.compliance_item_weight,
                "unit": self.compliance_item_weight_unit or "grams",
            }]
        if self.outer_material:
            _set_multi("outer_material", self.outer_material, True)
        if self.safety_warning:
            attrs["safety_warning"] = [{"value": self.safety_warning, "marketplace_id": mp}]
        if self.cpsia_warning and self.cpsia_warning != 'no_warning':
            attrs["cpsia_cautionary_statement"] = [{"value": self.cpsia_warning}]
        if self.battery_type and self.battery_type != 'none':
            attrs["battery_type"] = [{"value": self.battery_type}]

        # Remove empty values
        attrs = {k: v for k, v in attrs.items() if v}
        return attrs

    def _raise_if_invalid(self, result, action_title):
        """Check API result and raise with detailed errors if INVALID."""
        status = result.get('status', 'UNKNOWN')
        if status == 'INVALID':
            issues = result.get('issues', [])
            error_lines = []
            for issue in issues[:15]:
                code = issue.get('code', '')
                msg = issue.get('message', '')
                attr = issue.get('attributeNames', [])
                line = "[%s] %s" % (', '.join(attr), msg) if attr else msg
                if code:
                    line = "%s (%s)" % (line, code)
                error_lines.append(line)
            raise UserError(
                "Amazon rejected the request:\n\n%s" %
                ("\n".join(error_lines) or "No details returned by Amazon.")
            )
        return status

    # ══════════════════════════════════════════════════
    # Actions
    # ══════════════════════════════════════════════════

    def _find_odoo_products_by_sku(self):
        """Return all Odoo products whose Internal Reference matches Amazon SKU."""
        self.ensure_one()
        sku = (self.sku or '').strip()
        if not sku:
            return self.env['product.product']
        return self.env['product.product'].with_context(active_test=False).search([
            ('default_code', '=', sku),
        ])

    def _get_product_company(self):
        self.ensure_one()
        return self.instance_id.company_id or self.env.company

    def _prepare_odoo_template_vals(self):
        self.ensure_one()
        ProductTemplate = self.env['product.template']
        sku = (self.sku or '').strip()
        vals = {'name': (self.name or sku).strip()}
        if 'sale_ok' in ProductTemplate._fields:
            vals['sale_ok'] = True
        if 'purchase_ok' in ProductTemplate._fields:
            vals['purchase_ok'] = True
        if 'default_code' in ProductTemplate._fields:
            vals['default_code'] = sku
        if 'list_price' in ProductTemplate._fields:
            vals['list_price'] = self.amazon_price or 0.0
        if 'description_sale' in ProductTemplate._fields and self.description:
            vals['description_sale'] = self.description
        if 'company_id' in ProductTemplate._fields:
            vals['company_id'] = self._get_product_company().id
        if 'detailed_type' in ProductTemplate._fields:
            vals['detailed_type'] = 'consu'
        elif 'type' in ProductTemplate._fields:
            vals['type'] = 'consu'
        return vals

    def _create_odoo_product_from_amazon(self):
        """Create a stockable Odoo product from this Amazon marketplace record."""
        self.ensure_one()
        template = self.env['product.template'].create(self._prepare_odoo_template_vals())
        product = template.product_variant_id
        sku = (self.sku or '').strip()
        if sku and 'default_code' in product._fields and product.default_code != sku:
            product.default_code = sku
        return product

    def _update_odoo_price_from_amazon(self, product):
        self.ensure_one()
        price = self.amazon_price or 0.0
        template = product.product_tmpl_id
        if 'list_price' in template._fields:
            template.list_price = price
        elif 'list_price' in product._fields:
            product.list_price = price

    @staticmethod
    def _product_setup_message(stats):
        return (
            "%(processed)d processed, %(linked)d linked, %(created)d created, "
            "%(price_updated)d price updated, %(skipped)d skipped."
        ) % stats

    def _log_product_setup(self, operation, stats_by_instance):
        for data in stats_by_instance.values():
            errors = data['errors']
            summary = self._product_setup_message(data)
            log = self.env['amazon.sync.log'].log_start(
                data['instance'], operation,
                request_data={'record_ids': data['record_ids'][:100]},
            )
            if errors:
                log.log_partial(
                    summary=summary,
                    records_processed=data['processed'],
                    records_created=data['created'],
                    records_updated=data['linked'] + data['price_updated'],
                    records_failed=data['skipped'],
                    error_message="\n".join(errors[:50]),
                )
            else:
                log.log_success(
                    summary=summary,
                    records_processed=data['processed'],
                    records_created=data['created'],
                    records_updated=data['linked'] + data['price_updated'],
                    response_data={
                        'linked': data['linked'],
                        'created': data['created'],
                        'price_updated': data['price_updated'],
                        'skipped': data['skipped'],
                    },
                )

    def _setup_odoo_products(self, create_missing=False, update_prices_from_amazon=False):
        """Link by SKU, and optionally create missing Odoo products.

        Manual setup uses this helper; initial product sync can also create
        missing Odoo products when Amazon is the starting catalog.
        """
        stats = {
            'processed': 0,
            'linked': 0,
            'created': 0,
            'price_updated': 0,
            'skipped': 0,
            'errors': [],
        }
        stats_by_instance = {}

        def _instance_stats(product):
            instance = product.instance_id
            data = stats_by_instance.setdefault(instance.id, {
                'instance': instance,
                'record_ids': [],
                'processed': 0,
                'linked': 0,
                'created': 0,
                'price_updated': 0,
                'skipped': 0,
                'errors': [],
            })
            data['record_ids'].append(product.id)
            return data

        for rec in self:
            stats['processed'] += 1
            inst_stats = _instance_stats(rec)
            inst_stats['processed'] += 1

            if rec.odoo_product_id:
                if update_prices_from_amazon:
                    rec._update_odoo_price_from_amazon(rec.odoo_product_id)
                    stats['price_updated'] += 1
                    inst_stats['price_updated'] += 1
                else:
                    stats['skipped'] += 1
                    inst_stats['skipped'] += 1
                continue

            sku = (rec.sku or '').strip()
            if not sku:
                msg = "%s has no Amazon SKU. Cannot map or create an Odoo product." % (rec.display_name or rec.name)
                stats['errors'].append(msg)
                inst_stats['errors'].append(msg)
                stats['skipped'] += 1
                inst_stats['skipped'] += 1
                continue

            matches = rec._find_odoo_products_by_sku()
            if len(matches) > 1:
                msg = "SKU %s matches multiple Odoo products. Fix duplicate Internal References before mapping." % sku
                stats['errors'].append(msg)
                inst_stats['errors'].append(msg)
                stats['skipped'] += 1
                inst_stats['skipped'] += 1
                continue

            if len(matches) == 1:
                rec.odoo_product_id = matches.id
                stats['linked'] += 1
                inst_stats['linked'] += 1
                if update_prices_from_amazon:
                    rec._update_odoo_price_from_amazon(matches)
                    stats['price_updated'] += 1
                    inst_stats['price_updated'] += 1
                continue

            if not create_missing:
                stats['skipped'] += 1
                inst_stats['skipped'] += 1
                continue

            product = rec._create_odoo_product_from_amazon()
            rec.odoo_product_id = product.id
            stats['created'] += 1
            inst_stats['created'] += 1

        operation = 'product_create' if create_missing else 'product_mapping'
        if stats_by_instance:
            self._log_product_setup(operation, stats_by_instance)

        message = self._product_setup_message(stats)
        if stats['errors']:
            message += " %s" % " ".join(stats['errors'][:5])

        if stats['errors'] and not (stats['linked'] or stats['created'] or stats['price_updated']):
            raise UserError(message)

        has_warning = bool(stats['errors']) or (
            stats['skipped'] and not (stats['linked'] or stats['created'] or stats['price_updated'])
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Odoo Product Setup",
                "message": message,
                "type": "warning" if has_warning else "success",
                "sticky": bool(stats['errors']),
            },
        }

    def action_link_to_odoo_by_sku(self):
        """Link selected Amazon products to existing Odoo products by SKU/default_code."""
        return self._setup_odoo_products(create_missing=False)

    def action_create_odoo_product(self):
        """Create missing Odoo products for selected Amazon products."""
        return self._setup_odoo_products(create_missing=True)

    def action_map_to_odoo_product(self):
        """Backward-compatible form button: link existing products by SKU only."""
        return self.action_link_to_odoo_by_sku()

    # ══════════════════════════════════════════════════
    # AI Auto-Fill
    # ══════════════════════════════════════════════════

    # ══════════════════════════════════════════════════
    # Barcode / External Product ID Generator
    # ══════════════════════════════════════════════════

    def action_generate_barcode(self):
        """Generate a valid EAN-13 / UPC / GTIN barcode for this product."""
        import random
        self.ensure_one()

        barcode_type = self.barcode_type or 'EAN'

        if barcode_type == 'UPC':
            # UPC-A = 12 digits (11 + check digit)
            digits = [random.randint(0, 9) for _ in range(11)]
            check = self._calc_upc_check(digits)
            digits.append(check)
            code = ''.join(str(d) for d in digits)
        elif barcode_type == 'ISBN':
            # ISBN-13 starts with 978 or 979
            prefix = random.choice(['978', '979'])
            digits = [int(d) for d in prefix] + [random.randint(0, 9) for _ in range(9)]
            check = self._calc_ean_check(digits)
            digits.append(check)
            code = ''.join(str(d) for d in digits)
        else:
            # EAN-13 / GTIN-13 (default)
            # Use prefix 200-299 (internal use) to avoid conflicts with real products
            prefix = '2' + str(random.randint(0, 9)) + str(random.randint(0, 9))
            digits = [int(d) for d in prefix] + [random.randint(0, 9) for _ in range(9)]
            check = self._calc_ean_check(digits)
            digits.append(check)
            code = ''.join(str(d) for d in digits)

        self.barcode = code
        if not self.barcode_type:
            self.barcode_type = 'EAN'

    @staticmethod
    def _calc_ean_check(digits):
        """Calculate EAN-13 / GTIN-13 check digit from first 12 digits."""
        total = 0
        for i, d in enumerate(digits[:12]):
            total += d * (1 if i % 2 == 0 else 3)
        return (10 - (total % 10)) % 10

    @staticmethod
    def _calc_upc_check(digits):
        """Calculate UPC-A check digit from first 11 digits."""
        odd_sum = sum(digits[i] for i in range(0, 11, 2))
        even_sum = sum(digits[i] for i in range(1, 11, 2))
        total = odd_sum * 3 + even_sum
        return (10 - (total % 10)) % 10

    # ══════════════════════════════════════════════════
    # AI Helpers
    # ══════════════════════════════════════════════════

    def _get_ai_config(self):
        """Return (provider, api_key, model) from instance or raise."""
        instance = self.instance_id
        if not instance:
            raise UserError("No Amazon instance linked.")
        if not instance.ai_api_key:
            raise UserError(
                "AI API Key is not configured.\n"
                "Go to Amazon > Configuration > Instances > your instance > AI Auto-Fill section."
            )
        return instance.ai_provider or 'groq', instance.ai_api_key, instance.ai_model

    def _get_currency_name(self):
        inst = self.instance_id
        return inst._get_currency_code() if inst else self.env.company.currency_id.name

    # ══════════════════════════════════════════════════
    # 1. AI Product Listing Generator
    # ══════════════════════════════════════════════════

    def action_generate_with_ai(self):
        """Use AI to auto-fill all product fields from the product name."""
        self.ensure_one()
        if not self.name or len(self.name.strip()) < 2:
            raise UserError("Enter a product name first.")

        provider, api_key, model = self._get_ai_config()

        log = self.env['amazon.sync.log'].log_start(
            self.instance_id, 'ai_generate',
            res_model='amazon.product', res_id=self.id,
        )

        # Determine category for field-specific generation
        category_group = self.product_type_group or 'general'

        try:
            data = AmazonAIService.generate_listing(
                provider, api_key, model,
                product_name=self.name,
                brand=self.brand or '',
                product_type=self.product_type or '',
                barcode=self.barcode or '',
                currency=self._get_currency_name(),
                category_group=category_group,
            )
        except json.JSONDecodeError as exc:
            log.log_fail("AI returned invalid JSON: %s" % exc)
            raise UserError(
                "AI returned invalid JSON. Try again.\n\nDetails: %s" % exc
            ) from exc
        except Exception as exc:
            log.log_fail("AI generation failed: %s" % exc)
            raise UserError("AI generation failed: %s" % exc) from exc

        if not data or not isinstance(data, dict):
            log.log_fail("AI returned empty or invalid data")
            raise UserError("AI returned empty response. Please try again.")

        _logger.info("AI Generate [%s] — raw data keys: %s", category_group, list(data.keys()))

        # ── Map ALL fields from AI response ──
        updates = {}

        # ALL string/text/selection fields — one unified map
        all_str_fields = {
            # Product Identity
            'name': 'name', 'brand': 'brand', 'product_type': 'product_type',
            # Description
            'description': 'description', 'search_terms': 'search_terms',
            # Basic Info
            'manufacturer': 'manufacturer', 'model_name': 'model_name',
            'model_number': 'model_number', 'part_number': 'part_number',
            'color': 'color', 'color_map': 'color_map',
            'size': 'size', 'material': 'material',
            'age_range': 'age_range', 'item_type_name': 'item_type_name',
            'country_of_origin': 'country_of_origin',
            'occasion': 'occasion', 'special_features': 'special_features',
            'lifestyle': 'lifestyle', 'pattern': 'pattern',
            'style': 'style', 'seasons': 'seasons',
            'specific_uses': 'specific_uses',
            'subject_character': 'subject_character', 'theme': 'theme',
            'unit_count_type': 'unit_count_type',
            # Footwear
            'footwear_age_group': 'footwear_age_group',
            'footwear_width': 'footwear_width',
            'footwear_size': 'footwear_size',
            'sole_material': 'sole_material', 'toe_style': 'toe_style',
            'heel_type': 'heel_type', 'shoe_type': 'shoe_type',
            'closure_type': 'closure_type', 'height_map': 'height_map',
            'lining_description': 'lining_description',
            # Category Misc
            'embellishment_feature': 'embellishment_feature',
            'sport_type': 'sport_type',
            'water_resistance_level': 'water_resistance_level',
            # Selection fields (also handled here — auto-validated below)
            'target_gender': 'target_gender',
            'condition_type': 'condition_type',
            'footwear_size_system': 'footwear_size_system',
            'footwear_gender': 'footwear_gender',
            'footwear_size_class': 'footwear_size_class',
            'heel_height_unit': 'heel_height_unit',
            'barcode_type': 'barcode_type',
            # Compliance / Contact
            'manufacturer_contact': 'manufacturer_contact',
            'importer_contact': 'importer_contact',
            'packer_contact': 'packer_contact',
            # External Product Info (HSN)
            'external_info_entity': 'external_info_entity',
            'external_info_value': 'external_info_value',
            # Clothing
            'fit_type': 'fit_type',
            'department': 'department',
            'shirt_size': 'shirt_size',
            'care_instructions': 'care_instructions',
            'special_size_type': 'special_size_type',
            'sleeve_type': 'sleeve_type',
            'neck_style': 'neck_style',
        }
        for ai_key, odoo_field in all_str_fields.items():
            val = data.get(ai_key)
            if not val or not isinstance(val, str) or not val.strip():
                continue
            if odoo_field not in self._fields:
                continue
            val = val.strip()
            field_def = self._fields[odoo_field]

            # If it's a Selection field, validate the value
            if field_def.type == 'selection':
                sel = field_def.selection
                if callable(sel):
                    # Dynamic selection — skip validation, too risky
                    continue
                allowed = [k for k, _label in sel]
                if val in allowed:
                    updates[odoo_field] = val
                else:
                    # Try lowercase match
                    val_lower = val.lower().replace(' ', '_').replace('-', '_')
                    for k in allowed:
                        if k.lower() == val_lower:
                            updates[odoo_field] = k
                            break
                    # If still no match, skip silently
            else:
                updates[odoo_field] = val

        # ALL numeric fields
        numeric_fields = {
            'amazon_price': 'amazon_price', 'mrp': 'mrp', 'sale_price': 'sale_price',
            'amazon_qty': 'amazon_qty', 'handling_time': 'handling_time',
            'item_weight': 'item_weight', 'item_length': 'item_length',
            'item_width': 'item_width', 'item_height': 'item_height',
            'package_weight': 'package_weight', 'package_length': 'package_length',
            'package_width': 'package_width', 'package_height': 'package_height',
            'heel_height': 'heel_height', 'unit_count': 'unit_count',
            'number_of_items': 'number_of_items',
        }
        for ai_key, odoo_field in numeric_fields.items():
            val = data.get(ai_key)
            if val is not None:
                try:
                    fval = float(val)
                    if fval > 0 and odoo_field in self._fields:
                        updates[odoo_field] = fval
                except (ValueError, TypeError):
                    pass

        # Integer fields
        for int_field in ('amazon_qty', 'handling_time'):
            if int_field in updates:
                updates[int_field] = int(updates[int_field])

        # Fix pricing: ensure MRP > selling price (Amazon rule)
        if 'amazon_price' in updates and 'mrp' in updates:
            if updates['mrp'] <= updates['amazon_price']:
                # Swap — MRP must be higher
                updates['mrp'], updates['amazon_price'] = updates['amazon_price'], updates['mrp']
        elif 'amazon_price' in updates and 'mrp' not in updates:
            # Set MRP = 1.5x selling price if not provided
            updates['mrp'] = round(updates['amazon_price'] * 1.5, 2)

        _logger.info("AI Generate [%s] — writing %d fields: %s", category_group, len(updates), list(updates.keys()))

        if updates:
            self.write(updates)

        # Auto-generate barcode if missing
        if not self.barcode and not self.no_barcode:
            self.action_generate_barcode()

        # Bullet points
        bullet_points = data.get('bullet_points', [])
        if bullet_points and isinstance(bullet_points, list):
            self.bullet_point_ids.unlink()
            for i, bp in enumerate(bullet_points[:10]):
                if bp and isinstance(bp, str) and bp.strip():
                    self.env['amazon.product.bullet.point'].create({
                        'product_id': self.id,
                        'name': bp.strip(),
                        'sequence': (i + 1) * 10,
                    })

        filled = len(updates) + (1 if bullet_points else 0)
        log.log_success(summary="AI filled %d fields: %s" % (filled, ', '.join(updates.keys())))

        # Return form reload so user sees the updated fields immediately
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'amazon.product',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ══════════════════════════════════════════════════
    # 2. AI Smart Product Type Detection
    # ══════════════════════════════════════════════════

    def action_ai_detect_product_type(self):
        """Use AI to detect the correct Amazon productType (avoids error 4000003)."""
        self.ensure_one()
        if not self.name:
            raise UserError("Enter a product name first.")

        provider, api_key, model = self._get_ai_config()

        try:
            result = AmazonAIService.detect_product_type(
                provider, api_key, model,
                product_name=self.name,
                category=self.browse_node or '',
                brand=self.brand or '',
                description=self.description or '',
            )
        except Exception as exc:
            raise UserError("AI type detection failed: %s" % exc) from exc

        detected_type = result.get('product_type', '')
        confidence = result.get('confidence', 0)
        alternatives = result.get('alternatives', [])
        reasoning = result.get('reasoning', '')

        if detected_type:
            self.product_type = detected_type

        alt_text = ", ".join(alternatives[:5]) if alternatives else "None"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Product Type Detection",
                "message": "Detected: %s (confidence: %.0f%%)\nAlternatives: %s\n%s" % (
                    detected_type, confidence * 100, alt_text, reasoning,
                ),
                "type": "success" if confidence >= 0.7 else "warning",
                "sticky": True,
            },
        }

    # ══════════════════════════════════════════════════
    # 3. AI Price Optimization
    # ══════════════════════════════════════════════════

    def action_ai_optimize_price(self):
        """Use AI to suggest optimal pricing."""
        self.ensure_one()
        if not self.name:
            raise UserError("Enter a product name first.")

        provider, api_key, model = self._get_ai_config()
        currency = self._get_currency_name()

        # Gather competitor pricing if available via API
        competitor_info = ''
        try:
            api = AmazonAPI()
            access_token = self.instance_id._get_access_token_or_raise()
            if self.asin:
                comp_data = api.get_competitive_pricing(self.instance_id, access_token, self.asin)
                prices = comp_data.get('payload', [])
                if prices:
                    comp_lines = []
                    for p in prices[:5]:
                        cp = p.get('Product', {}).get('CompetitivePricing', {}).get('CompetitivePrices', [])
                        for c in cp:
                            amt = c.get('Price', {}).get('ListingPrice', {}).get('Amount', '')
                            if amt:
                                comp_lines.append(str(amt))
                    competitor_info = ", ".join(comp_lines) if comp_lines else ''
        except Exception:
            pass  # Non-critical — AI can work without competitor data

        cost_price = 0
        if self.odoo_product_id:
            cost_price = self.odoo_product_id.standard_price or 0

        try:
            result = AmazonAIService.optimize_price(
                provider, api_key, model,
                product_name=self.name,
                category=self.product_type or '',
                current_price=self.amazon_price or 0,
                cost_price=cost_price,
                competitor_prices=competitor_info,
                currency=currency,
            )
        except Exception as exc:
            raise UserError("AI pricing failed: %s" % exc) from exc

        suggested = result.get('suggested_price', 0)
        strategy = result.get('strategy', '')
        reasoning = result.get('reasoning', '')
        recommendations = result.get('recommendations', [])

        msg_parts = [
            "Suggested Price: %s %.2f" % (currency, suggested),
            "Strategy: %s" % strategy,
            "Range: %.2f - %.2f" % (result.get('min_price', 0), result.get('max_price', 0)),
        ]
        if reasoning:
            msg_parts.append("Reasoning: %s" % reasoning[:200])
        if recommendations:
            msg_parts.append("Tips: %s" % "; ".join(recommendations[:3]))

        # Auto-apply suggested price if > 0
        if suggested > 0:
            self.amazon_price = suggested

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Price Optimization",
                "message": "\n".join(msg_parts),
                "type": "success",
                "sticky": True,
            },
        }

    # ══════════════════════════════════════════════════
    # 4. AI Inventory Prediction
    # ══════════════════════════════════════════════════

    def action_ai_predict_inventory(self):
        """Use AI to predict demand and suggest reorder quantities."""
        self.ensure_one()
        if not self.name:
            raise UserError("Enter a product name first.")

        provider, api_key, model = self._get_ai_config()

        # Calculate sales history from Odoo
        current_stock = self.odoo_stock or self.amazon_qty or 0
        avg_daily = 0
        sales_history = ''
        if self.odoo_product_id:
            # Get last 12 weeks of sales data
            from datetime import timedelta
            today = fields.Date.today()
            weekly_sales = []
            for week in range(12):
                start = today - timedelta(days=(week + 1) * 7)
                end = today - timedelta(days=week * 7)
                qty = sum(
                    self.env['sale.order.line'].search([
                        ('product_id', '=', self.odoo_product_id.id),
                        ('order_id.state', 'in', ['sale', 'done']),
                        ('order_id.date_order', '>=', start),
                        ('order_id.date_order', '<', end),
                    ]).mapped('product_uom_qty')
                )
                weekly_sales.append(qty)
            weekly_sales.reverse()  # oldest first
            sales_history = ", ".join(["%.0f" % s for s in weekly_sales])
            total_days = min(84, 84)  # 12 weeks
            total_sold = sum(weekly_sales)
            avg_daily = total_sold / total_days if total_days > 0 else 0

        try:
            result = AmazonAIService.predict_inventory(
                provider, api_key, model,
                product_name=self.name,
                current_stock=current_stock,
                avg_daily_sales=round(avg_daily, 2),
                lead_time=7,
                sales_history=sales_history or 'Not available',
            )
        except Exception as exc:
            raise UserError("AI inventory prediction failed: %s" % exc) from exc

        msg_parts = [
            "Current Stock: %.0f" % current_stock,
            "Predicted Daily Demand: %.1f" % result.get('predicted_daily_demand', 0),
            "Days of Stock: %d" % result.get('days_of_stock_remaining', 0),
            "Reorder Point: %d" % result.get('reorder_point', 0),
            "Suggested Reorder Qty: %d" % result.get('suggested_reorder_qty', 0),
            "Stockout Risk: %s" % result.get('stockout_risk', 'unknown'),
            "Trend: %s" % result.get('demand_trend', 'unknown'),
        ]
        reasoning = result.get('reasoning', '')
        if reasoning:
            msg_parts.append("Analysis: %s" % reasoning[:200])

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "AI Inventory Prediction",
                "message": "\n".join(msg_parts),
                "type": "warning" if result.get('stockout_risk') == 'high' else "success",
                "sticky": True,
            },
        }

    # ══════════════════════════════════════════════════
    # 6. AI Error Fixer (called from create/update flows)
    # ══════════════════════════════════════════════════

    def _ai_analyze_error(self, endpoint, method, status_code, error_body, request_body):
        """Analyze an Amazon API error using AI and optionally apply fixes."""
        try:
            provider, api_key, model = self._get_ai_config()
        except UserError:
            return None  # AI not configured — skip

        product_info = "Name: %s, SKU: %s, Type: %s, Brand: %s" % (
            self.name, self.sku, self.product_type, self.brand,
        )
        try:
            result = AmazonAIService.analyze_and_fix_error(
                provider, api_key, model,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                error_body=error_body,
                request_summary=json.dumps(request_body, default=str)[:1500] if request_body else '',
                product_info=product_info,
            )
        except Exception as exc:
            _logger.warning("AI error analysis failed: %s", exc)
            return None

        # Auto-apply field fixes if AI suggests them
        field_fixes = result.get('field_fixes', {})
        applied = []
        if field_fixes and isinstance(field_fixes, dict):
            writable = {}
            for field_name, value in field_fixes.items():
                if field_name in self._fields and value:
                    writable[field_name] = value
                    applied.append("%s → %s" % (field_name, value))
            if writable:
                try:
                    self.write(writable)
                except Exception as exc:
                    _logger.warning("AI fix auto-apply failed: %s", exc)

        result['applied_fixes'] = applied
        return result

    def action_search_product_type(self):
        """Search Amazon for valid product types based on current product name/type."""
        self.ensure_one()
        if not self.instance_id:
            raise UserError("No Amazon instance linked.")

        api = AmazonAPI()
        access_token = self.instance_id._get_access_token_or_raise()

        keyword = self.product_type or self.name or ''
        if not keyword:
            raise UserError("Enter a Product Type or Item Name first to search.")

        try:
            data = api.search_product_type_definitions(self.instance_id, access_token, keyword)
        except Exception as exc:
            raise UserError("Failed to search product types: %s" % exc) from exc

        types = data.get('productTypes', [])
        if not types:
            raise UserError(
                "No product types found for '%s'.\n\n"
                "Try broader keywords like: shoes, shirt, electronics, toy, book" % keyword
            )

        # Show results to user
        lines = []
        for pt in types[:20]:
            name = pt.get('name', '')
            display = pt.get('displayName', '')
            mps = ', '.join(pt.get('marketplaceIds', []))
            lines.append("  %s  —  %s" % (name, display))

        raise UserError(
            "Valid product types for '%s':\n\n%s\n\n"
            "Copy the exact value (left column) into the Product Type field." % (
                keyword, "\n".join(lines)
            )
        )

    def _auto_fill_missing_fields(self):
        """Auto-fill required fields before pushing to Amazon. No more validation errors."""
        updates = {}

        # Barcode
        if not self.barcode and not self.no_barcode:
            self.action_generate_barcode()

        # Brand
        if not self.brand:
            updates['brand'] = self.manufacturer or self.name.split()[0] if self.name else 'Generic'

        # Price logic: MRP > selling price
        if self.amazon_price and self.mrp and self.mrp <= self.amazon_price:
            updates['mrp'] = round(self.amazon_price * 1.5, 2)
        if not self.mrp and self.amazon_price:
            updates['mrp'] = round(self.amazon_price * 1.5, 2)
        if not self.amazon_price and self.mrp:
            updates['amazon_price'] = round(self.mrp * 0.7, 2)

        # Model number
        if not self.model_number:
            updates['model_number'] = self.sku or 'N/A'

        # Country of origin
        if not self.country_of_origin:
            updates['country_of_origin'] = 'IN'

        # Manufacturer
        if not self.manufacturer:
            updates['manufacturer'] = self.brand or 'Generic'

        # Material
        if not self.material:
            updates['material'] = 'Synthetic'

        # Color
        if not self.color:
            updates['color'] = 'Black'

        # Target gender
        if not self.target_gender:
            updates['target_gender'] = 'Unisex'

        # Item type name
        if not self.item_type_name:
            updates['item_type_name'] = self.product_type or 'General'

        # HSN
        if not self.external_info_entity:
            updates['external_info_entity'] = 'HSN'
        if not self.external_info_value:
            ptype = (self.product_type or '').upper()
            if any(k in ptype for k in ['SHOE', 'SANDAL', 'BOOT', 'SNEAKER']):
                updates['external_info_value'] = '64041100'
            elif any(k in ptype for k in ['SHIRT', 'DRESS', 'PANT', 'JEAN']):
                updates['external_info_value'] = '62052000'
            else:
                updates['external_info_value'] = '63079090'

        # Importer/Packer
        if not self.importer_contact:
            updates['importer_contact'] = self.manufacturer_contact or 'Contact seller for details'
        if not self.packer_contact:
            updates['packer_contact'] = self.manufacturer_contact or 'Contact seller for details'

        # Footwear-specific
        ptype_upper = (self.product_type or '').upper()
        FWKW = ['SHOE', 'SANDAL', 'BOOT', 'SNEAKER', 'SLIPPER', 'FOOTWEAR', 'ATHLETIC', 'LOAFER']
        is_fw = any(kw in ptype_upper for kw in FWKW)
        if is_fw:
            if not self.footwear_size:
                updates['footwear_size'] = self.size or '7'
            # Always force correct size system for India
            is_india = (self.instance_id.marketplace_id == 'A21TJRUUN4KGV') if self.instance_id else False
            if is_india:
                updates['footwear_size_system'] = 'IND'
            elif not self.footwear_size_system:
                updates['footwear_size_system'] = 'EU'
            if not self.heel_type:
                updates['heel_type'] = 'No Heel'
            if not self.closure_type:
                updates['closure_type'] = 'Pull On'
            if not self.sole_material:
                updates['sole_material'] = 'Rubber'
            if not self.style:
                updates['style'] = 'Casual'
            if not self.height_map:
                updates['height_map'] = 'Ankle'
            if not self.outer_material:
                updates['outer_material'] = self.material or 'Synthetic'
            if not self.water_resistance_level:
                updates['water_resistance_level'] = 'Not Water Resistant'
            if not self.manufacturer_contact:
                updates['manufacturer_contact'] = 'Contact seller for details'

        # Clothing-specific
        CLOTHING_KW = ['SHIRT', 'DRESS', 'PANT', 'JEAN', 'JACKET', 'KURTA', 'SAREE', 'SUIT']
        is_clothing = any(kw in ptype_upper for kw in CLOTHING_KW)
        if is_clothing:
            if not self.fit_type:
                updates['fit_type'] = 'Regular Fit'
            if not self.department:
                updates['department'] = self.target_gender or 'Men'
            if not self.number_of_items:
                updates['number_of_items'] = 1
            if not self.shirt_size:
                updates['shirt_size'] = self.size or 'M'
            if not self.care_instructions:
                updates['care_instructions'] = 'Machine Wash'
            if not self.special_size_type:
                updates['special_size_type'] = 'Standard'
            if not self.style:
                updates['style'] = 'Casual'
            if not self.sleeve_type:
                updates['sleeve_type'] = 'Half Sleeve'
            if not self.unit_count_type:
                updates['unit_count_type'] = 'Unit'

        # General
        if not self.unit_count_type:
            updates['unit_count_type'] = 'Pair' if is_fw else 'Unit'

        # Dimensions
        if not self.item_weight:
            updates['item_weight'] = 0.5
        if not self.item_length:
            updates['item_length'] = 30.0
        if not self.item_width:
            updates['item_width'] = 20.0
        if not self.item_height:
            updates['item_height'] = 12.0

        # Quantity
        if not self.amazon_qty:
            updates['amazon_qty'] = 10

        if updates:
            _logger.info("Auto-filling %d missing fields before Amazon push: %s", len(updates), list(updates.keys()))
            self.write(updates)

    def _auto_fix_product_type(self, api, access_token):
        """Validate product type against Amazon API. If invalid, search and auto-fix."""
        self.ensure_one()
        if not self.product_type:
            return

        keyword = self.product_type
        try:
            data = api.search_product_type_definitions(self.instance_id, access_token, keyword)
            types = data.get('productTypes', [])

            if not types:
                name_keyword = (self.name or '').split()[0] if self.name else keyword
                data = api.search_product_type_definitions(self.instance_id, access_token, name_keyword)
                types = data.get('productTypes', [])

            if types:
                valid_names = [t.get('name', '') for t in types]
                if self.product_type not in valid_names:
                    correct_type = types[0].get('name', '')
                    if correct_type and correct_type != self.product_type:
                        _logger.info("Auto-fixing product type: %s → %s", self.product_type, correct_type)
                        self.product_type = correct_type

        except Exception as exc:
            _logger.warning("Product type validation failed: %s", exc)

    def _fetch_required_attributes(self, api, access_token):
        """Fetch required attributes from Amazon Product Type Definition API."""
        self.ensure_one()
        if not self.product_type:
            return []
        try:
            data = api.get_product_type_definition(self.instance_id, access_token, self.product_type)
            schema = data.get('schema', {}).get('properties', {})
            required = []
            for attr_name, attr_def in schema.items():
                if attr_def.get('required'):
                    required.append(attr_name)
            _logger.info("Required attributes for %s: %s", self.product_type, required[:30])
            return required
        except Exception as exc:
            _logger.warning("Failed to fetch product type definition: %s", exc)
            return []

    def _ai_fill_missing_amazon_attrs(self, attrs, required_attrs, api, access_token):
        """Use AI to fill any required Amazon attributes that are missing from the payload."""
        missing = [a for a in required_attrs if a not in attrs]
        if not missing:
            return attrs

        _logger.info("Missing %d required attrs: %s", len(missing), missing[:20])

        # Use AI to generate values for missing attributes
        try:
            provider, api_key, model = self._get_ai_config()
        except Exception:
            return attrs  # AI not configured

        from ..services.ai_service import AmazonAIService

        mp = self.instance_id.marketplace_id
        prompt = """You are an Amazon SP-API expert. Fill these missing REQUIRED attributes for an Amazon listing.

Product: {name}
Brand: {brand}
Product Type: {ptype}
Category: {category}
Material: {material}
Color: {color}

Missing required attributes: {missing}

Return ONLY valid JSON where each key is an attribute name and value is the Amazon-compatible value.
For nested attributes (like shirt_size, footwear_size), return a simple string value.
Example: {{"batteries_required": "No", "warranty_description": "1 Year Manufacturer Warranty", "included_components": "1 Unit", "connectivity_technology": "Bluetooth"}}

Rules:
- Fill EVERY attribute with a realistic value
- Use simple string values unless the attribute clearly needs a number
- Return ONLY the JSON object""".format(
            name=self.name or '',
            brand=self.brand or '',
            ptype=self.product_type or '',
            category=self.product_type_group or 'general',
            material=self.material or '',
            color=self.color or '',
            missing=', '.join(missing[:30]),
        )

        try:
            ai_data = AmazonAIService._call_and_parse(provider, api_key, model, prompt)
        except Exception as exc:
            _logger.warning("AI fill missing attrs failed: %s", exc)
            return attrs

        # Add AI-generated values to attrs
        for attr_name, value in ai_data.items():
            if attr_name not in attrs and attr_name in missing:
                if isinstance(value, str):
                    attrs[attr_name] = [{"value": value, "marketplace_id": mp}]
                elif isinstance(value, (int, float)):
                    attrs[attr_name] = [{"value": value}]
                elif isinstance(value, list):
                    attrs[attr_name] = value
                else:
                    attrs[attr_name] = [{"value": str(value), "marketplace_id": mp}]

        _logger.info("AI filled %d missing attrs", len([k for k in ai_data if k in missing]))
        return attrs

    def action_create_in_amazon(self):
        """Create a new listing on Amazon. Auto-fills missing required fields first."""
        self.ensure_one()
        if not self.sku:
            raise UserError("SKU is required.")
        if not self.instance_id:
            raise UserError("No Amazon instance linked.")
        if not self.product_type:
            raise UserError(
                "Product Type is required.\n"
                "Examples: SHOES, SHIRT, HOME_BED_AND_BATH\n"
                "Use 'AI Detect Type' button or find your type at Amazon Seller Central > Add a Product."
            )
        if not self.name:
            raise UserError("Product name is required.")

        # Auto-fill any missing required fields before sending
        self._auto_fill_missing_fields()

        api = AmazonAPI()
        access_token = self.instance_id._get_access_token_or_raise()

        # Auto-validate product type — search Amazon for the correct type
        self._auto_fix_product_type(api, access_token)

        # Build base attributes from product fields
        attrs = self._build_amazon_attributes()

        # Fetch required attributes from Amazon and use AI to fill any missing ones
        required_attrs = self._fetch_required_attributes(api, access_token)
        if required_attrs:
            attrs = self._ai_fill_missing_amazon_attrs(attrs, required_attrs, api, access_token)
        body = {
            "productType": self.product_type,
            "requirements": "LISTING",
            "attributes": attrs,
        }

        log = self.env['amazon.sync.log'].log_start(
            self.instance_id, 'product_create',
            request_data={'sku': self.sku, 'product_type': self.product_type, 'attr_keys': list(attrs.keys())},
            res_model='amazon.product', res_id=self.id,
        )

        _logger.info("Create in Amazon — SKU: %s, productType: %s, %d attributes: %s",
                      self.sku, self.product_type, len(attrs), list(attrs.keys()))
        # Debug: log full payload for troubleshooting
        _logger.info("Create in Amazon — Full payload:\n%s", json.dumps(body, indent=2, default=str)[:5000])

        try:
            result = api.put_listings_item(self.instance_id, access_token, self.sku, body)
        except requests.exceptions.HTTPError as exc:
            resp_text = ''
            status_code = '?'
            if exc.response is not None:
                resp_text = exc.response.text
                status_code = exc.response.status_code
            diagnostic = getattr(exc, 'amazon_diagnostic', None) or AmazonAPI.format_exception(exc)

            # AI Error Fixer: analyze and suggest fix
            ai_fix = self._ai_analyze_error(
                endpoint='listings/items', method='PUT',
                status_code=status_code, error_body=resp_text, request_body=body,
            )
            fix_msg = ''
            if ai_fix:
                fix_msg = "\n\nAI Analysis: %s" % ai_fix.get('root_cause', '')
                if ai_fix.get('applied_fixes'):
                    fix_msg += "\nAuto-applied fixes: %s" % ", ".join(ai_fix['applied_fixes'])
                fix_msg += "\nSuggested fix: %s" % ai_fix.get('fix_description', '')

            log.log_fail(diagnostic)
            raise UserError(
                "Amazon rejected the request:\n\n%s%s" %
                (diagnostic, fix_msg)
            ) from exc
        except Exception as exc:
            log.log_fail(str(exc))
            raise UserError("Failed to create listing: %s" % exc) from exc

        if not result:
            log.log_fail("Empty response from Amazon.")
            raise UserError("Amazon returned an empty response.")

        self.last_sync_date = fields.Datetime.now()
        status = result.get('status', 'UNKNOWN')

        # INVALID = rejected with issues — try AI error fixer
        if status == 'INVALID':
            issues = result.get('issues', [])
            error_text = json.dumps(issues[:10], default=str)

            ai_fix = self._ai_analyze_error(
                endpoint='listings/items', method='PUT',
                status_code='200-INVALID', error_body=error_text, request_body=body,
            )
            fix_msg = ''
            if ai_fix:
                fix_msg = "\n\nAI Analysis: %s" % ai_fix.get('root_cause', '')
                if ai_fix.get('applied_fixes'):
                    fix_msg += "\nAuto-applied: %s (click Create again to retry)" % ", ".join(ai_fix['applied_fixes'])
                fix_msg += "\nSuggested: %s" % ai_fix.get('fix_description', '')

            log.log_fail("INVALID: %s" % error_text[:500], response_data=result)

            # Build readable error
            error_lines = []
            for issue in issues[:15]:
                code = issue.get('code', '')
                msg = issue.get('message', '')
                attr = issue.get('attributeNames', [])
                line = "[%s] %s" % (', '.join(attr), msg) if attr else msg
                if code:
                    line = "%s (%s)" % (line, code)
                error_lines.append(line)
            raise UserError(
                "Amazon rejected the request:\n\n%s%s" %
                ("\n".join(error_lines) or "No details returned.", fix_msg)
            )

        # ACCEPTED / VALID = success
        msg = "Listing submitted. Status: %s" % status
        if status == 'ACCEPTED':
            msg = "Listing created successfully! It may take a few minutes to appear on Amazon."
            if result.get('asin'):
                self.asin = result['asin']
            self.status = 'Active'

        log.log_success(
            summary="Status: %s, ASIN: %s" % (status, self.asin or 'pending'),
            records_created=1, response_data=result,
        )

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Create in Amazon",
                "message": msg,
                "type": "success" if status in ('ACCEPTED', 'VALID') else "warning",
                "sticky": status not in ('ACCEPTED', 'VALID'),
            },
        }

    def action_update_in_amazon(self):
        """Push updated product data to existing Amazon listing using PATCH."""
        self.ensure_one()
        if not self.sku:
            raise UserError("SKU is required.")
        if not self.instance_id:
            raise UserError("No Amazon instance linked.")

        api = AmazonAPI()
        access_token = self.instance_id._get_access_token_or_raise()

        attrs = self._build_amazon_attributes()

        # Build PATCH body with patches array
        patches = []
        for key, value in attrs.items():
            patches.append({
                "op": "replace",
                "path": "/attributes/%s" % key,
                "value": value,
            })

        body = {
            "productType": self.product_type or "PRODUCT",
            "patches": patches,
        }

        log = self.env['amazon.sync.log'].log_start(
            self.instance_id, 'product_update',
            request_data={'sku': self.sku, 'patch_count': len(patches)},
            res_model='amazon.product', res_id=self.id,
        )

        try:
            result = api.patch_listings_item(self.instance_id, access_token, self.sku, body)
        except Exception as exc:
            log.log_fail(str(exc))
            raise UserError("Failed to update listing: %s" % exc) from exc

        # Also push stock for FBM. Never send FBA/AFN stock as MFN inventory.
        if self.fulfillment_channel == 'MFN' and self.odoo_product_id:
            try:
                from .amazon_api import FEED_JSON_LISTINGS
                qty = self.instance_id._get_stock_qty_for_amazon_export(self.odoo_product_id)
                content = api.build_inventory_json_feed(self.instance_id, [{
                    'sku': self.sku,
                    'quantity': qty,
                    'product_type': self.product_type or 'PRODUCT',
                }])
                api.submit_feed(
                    self.instance_id, access_token, FEED_JSON_LISTINGS, content,
                    content_type='application/json; charset=UTF-8',
                )
            except Exception as exc:
                _logger.warning("Stock update failed for %s: %s", self.sku, exc)
        elif self.fulfillment_channel == 'AFN':
            _logger.info("Skipping MFN stock feed for FBA/AFN product %s.", self.sku)

        self.last_sync_date = fields.Datetime.now()

        status_val = result.get('status', 'UNKNOWN') if result else 'UNKNOWN'
        if status_val == 'INVALID':
            log.log_fail("INVALID response", response_data=result)
            self._raise_if_invalid(result, "Update in Amazon")
        else:
            log.log_success(summary="Status: %s" % status_val, records_updated=1, response_data=result)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Update in Amazon",
                "message": "Listing updated. Status: %s" % status_val,
                "type": "success",
                "sticky": False,
            },
        }

    def action_pull_from_amazon(self):
        """Pull all listing data from Amazon into this record."""
        self.ensure_one()
        if not self.sku:
            raise UserError("SKU is required.")
        if not self.instance_id:
            raise UserError("No Amazon instance linked.")

        api = AmazonAPI()
        access_token = self.instance_id._get_access_token_or_raise()

        try:
            data = api.get_listings_item(self.instance_id, access_token, self.sku)
        except Exception as exc:
            raise UserError("Failed to pull from Amazon: %s" % exc) from exc

        _logger.info("Pull from Amazon for SKU %s — response keys: %s", self.sku, list(data.keys()))

        updates = {}  # Collect all field updates to write at once

        # ══════════════════════════════════════════════════
        # Summaries
        # ══════════════════════════════════════════════════
        summaries = data.get('summaries', [])
        if summaries:
            s = summaries[0]
            _logger.info("Summaries: %s", s)
            if s.get('itemName'):
                updates['name'] = s['itemName']
            if s.get('asin'):
                updates['asin'] = s['asin']
            if s.get('mainImage', {}).get('link'):
                updates['image_url'] = s['mainImage']['link']
            status = s.get('status', '')
            if status in ('Active', 'Inactive', 'Incomplete'):
                updates['status'] = status
            if s.get('productType'):
                updates['product_type'] = s['productType']
            if s.get('browseClassification', {}).get('displayName'):
                updates['browse_node'] = s['browseClassification']['displayName']
            if s.get('brand'):
                updates['brand'] = s['brand']
            if s.get('color'):
                updates['color'] = s['color']
            if s.get('size'):
                updates['size'] = s['size']

        # ══════════════════════════════════════════════════
        # Attributes
        # ══════════════════════════════════════════════════
        a = data.get('attributes', {})
        if a:
            _logger.info("Attributes keys: %s", list(a.keys()))

            # --- Product Identity ---
            self._pull_val(a, 'brand', updates, 'brand')
            self._pull_val(a, 'item_name', updates, 'name')
            self._pull_val(a, 'externally_assigned_product_identifier', updates, 'barcode', sub_key='value')

            # --- Variations ---
            vt = a.get('variation_theme', [])
            if vt and isinstance(vt[0], dict):
                theme_name = vt[0].get('name', '')
                if theme_name:
                    updates['has_variations'] = True
                    updates['variation_theme'] = theme_name
                    # Auto-check the matching checkboxes
                    theme_map = {
                        'TeamName': 'var_team_name',
                        'Athlete': 'var_athlete',
                        'Color': 'var_color',
                        'NumberOfItems': 'var_number_of_items',
                        'FootwearSize': 'var_footwear_size',
                        'Size': 'var_size',
                        'Material': 'var_material',
                        'Pattern': 'var_pattern',
                        'Style': 'var_style',
                    }
                    # Reset all checkboxes first
                    for field in theme_map.values():
                        updates[field] = False
                    # Set the ones from Amazon
                    for part in theme_name.split('/'):
                        part = part.strip()
                        if part in theme_map:
                            updates[theme_map[part]] = True

            parent_rel = a.get('child_parent_sku_relationship', [])
            if parent_rel and isinstance(parent_rel[0], dict):
                updates['parent_asin'] = parent_rel[0].get('parent_sku', '')

            # --- Description ---
            self._pull_val(a, 'product_description', updates, 'description')
            self._pull_val(a, 'generic_keyword', updates, 'search_terms')

            # Bullet points
            bps = a.get('bullet_point', [])
            if bps:
                self.bullet_point_ids.unlink()
                for i, bp in enumerate(bps):
                    val = bp.get('value', '') if isinstance(bp, dict) else str(bp)
                    if val:
                        self.env['amazon.product.bullet.point'].create({
                            'product_id': self.id,
                            'name': val,
                            'sequence': (i + 1) * 10,
                        })

            # --- Product Details ---
            self._pull_val(a, 'manufacturer', updates, 'manufacturer')
            self._pull_val(a, 'model_number', updates, 'model_number')
            self._pull_val(a, 'model_name', updates, 'model_name')
            self._pull_val(a, 'part_number', updates, 'part_number')
            self._pull_multi(a, 'special_feature', updates, 'special_features')
            self._pull_val(a, 'lifestyle', updates, 'lifestyle')
            self._pull_val(a, 'style', updates, 'style')
            self._pull_val(a, 'target_gender', updates, 'target_gender')
            self._pull_val(a, 'age_range_description', updates, 'age_range')
            self._pull_val(a, 'lining_description', updates, 'lining_description')
            self._pull_val(a, 'pattern', updates, 'pattern')
            self._pull_val(a, 'item_type_name', updates, 'item_type_name')
            self._pull_val(a, 'water_resistance_level', updates, 'water_resistance_level')
            self._pull_val(a, 'subject_character', updates, 'subject_character')
            self._pull_val(a, 'color_map', updates, 'color_map')
            self._pull_val(a, 'color', updates, 'color')
            self._pull_val(a, 'size', updates, 'size')
            self._pull_val(a, 'material', updates, 'material')
            self._pull_val(a, 'country_of_origin', updates, 'country_of_origin')

            # Footwear
            self._pull_val(a, 'size_system', updates, 'footwear_size_system')
            self._pull_val(a, 'size_class', updates, 'footwear_size_class')
            self._pull_val(a, 'width', updates, 'footwear_width')
            self._pull_val(a, 'footwear_size', updates, 'footwear_size')

            # Category-specific
            self._pull_multi(a, 'occasion_type', updates, 'occasion')
            self._pull_multi(a, 'theme', updates, 'theme')
            self._pull_val(a, 'sole_material', updates, 'sole_material')
            self._pull_val(a, 'toe_style', updates, 'toe_style')
            self._pull_val(a, 'manufacturer_contact_information', updates, 'manufacturer_contact')
            self._pull_val(a, 'height_map', updates, 'height_map')
            self._pull_multi(a, 'specific_uses_for_product', updates, 'specific_uses')
            self._pull_multi(a, 'sport_type', updates, 'sport_type')
            self._pull_multi(a, 'embellishment_feature', updates, 'embellishment_feature')
            self._pull_multi(a, 'heel_type', updates, 'heel_type')
            self._pull_multi(a, 'seasons', updates, 'seasons')
            self._pull_val(a, 'importer_contact_information', updates, 'importer_contact')
            self._pull_val(a, 'packer_contact_information', updates, 'packer_contact')
            self._pull_multi(a, 'closure_type', updates, 'closure_type')
            self._pull_val(a, 'shoe_type', updates, 'shoe_type')

            # Unit count
            self._pull_numeric(a, 'unit_count', updates, 'unit_count')
            self._pull_val(a, 'unit_count_type', updates, 'unit_count_type')

            # Heel
            self._pull_numeric(a, 'heel_height', updates, 'heel_height')

            # Item weight (both Product Details and Safety tabs)
            weight = a.get('item_weight', [])
            if weight and isinstance(weight[0], dict):
                w = weight[0]
                val = w.get('value')
                if val:
                    updates['item_weight'] = float(val)
                    updates['compliance_item_weight'] = float(val)
                    unit = w.get('unit', '')
                    if unit in ('grams', 'kilograms', 'pounds', 'ounces'):
                        updates['compliance_item_weight_unit'] = unit

            # Offer / Price
            price_list = a.get('purchasable_offer', [])
            if price_list and isinstance(price_list[0], dict):
                try:
                    updates['amazon_price'] = float(price_list[0]['our_price'][0]['schedule'][0]['value_with_tax'])
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            self._pull_numeric(a, 'list_price', updates, 'mrp')
            self._pull_numeric(a, 'sale_price', updates, 'sale_price')

            # Condition
            self._pull_val(a, 'condition_type', updates, 'condition_type')
            self._pull_val(a, 'condition_note', updates, 'condition_note')

            # Item Dimensions (Offer tab)
            self._pull_dimension(a, 'item_length', updates, 'offer_item_length', 'offer_item_length_unit')
            self._pull_dimension(a, 'item_width', updates, 'offer_item_width', 'offer_item_width_unit')
            self._pull_dimension(a, 'item_height', updates, 'offer_item_height', 'offer_item_height_unit')

            # Item/Package dimensions (Product Details tab)
            dims = a.get('item_dimensions', [])
            if dims and isinstance(dims[0], dict):
                d = dims[0]
                for dim_key, field_name in [('length', 'item_length'), ('width', 'item_width'), ('height', 'item_height')]:
                    if dim_key in d and isinstance(d[dim_key], dict):
                        updates[field_name] = float(d[dim_key].get('value', 0))
            pkg_dims = a.get('item_package_dimensions', [])
            if pkg_dims and isinstance(pkg_dims[0], dict):
                d = pkg_dims[0]
                for dim_key, field_name in [('length', 'package_length'), ('width', 'package_width'), ('height', 'package_height')]:
                    if dim_key in d and isinstance(d[dim_key], dict):
                        updates[field_name] = float(d[dim_key].get('value', 0))
            self._pull_numeric(a, 'item_package_weight', updates, 'package_weight')

            # Max order qty & tax code
            self._pull_numeric(a, 'max_order_quantity', updates, 'max_order_qty')
            self._pull_val(a, 'product_tax_code', updates, 'tax_code')

            # Safety & Compliance
            self._pull_val(a, 'country_of_origin', updates, 'compliance_country_of_origin')
            self._pull_multi(a, 'outer_material', updates, 'outer_material')
            self._pull_val(a, 'safety_warning', updates, 'safety_warning')
            self._pull_val(a, 'cpsia_cautionary_statement', updates, 'cpsia_warning')
            self._pull_val(a, 'battery_type', updates, 'battery_type')

            # External product info
            ext_info = a.get('external_product_information', [])
            if ext_info and isinstance(ext_info[0], dict):
                updates['external_info_entity'] = ext_info[0].get('external_product_information_entity', '')
                updates['external_info_value'] = ext_info[0].get('external_product_information_value', '')

        # ══════════════════════════════════════════════════
        # Fulfillment Availability
        # ══════════════════════════════════════════════════
        fulfillment = data.get('fulfillmentAvailability', [])
        if fulfillment:
            fa = fulfillment[0]
            _logger.info("Fulfillment: %s", fa)
            updates['amazon_qty'] = fa.get('quantity', 0)
            fc = fa.get('fulfillmentChannelCode', '')
            # DEFAULT = Merchant Fulfilled, AMAZON_* = FBA
            if fc in ('AMAZON_NA', 'AMAZON_EU', 'AMAZON_FE', 'AMAZON_IN'):
                updates['fulfillment_channel'] = 'AFN'
            else:
                updates['fulfillment_channel'] = 'MFN'

        # ══════════════════════════════════════════════════
        # Offers (pricing from offers section)
        # ══════════════════════════════════════════════════
        offers = data.get('offers', [])
        if offers and isinstance(offers[0], dict):
            o = offers[0]
            if o.get('price', {}).get('Amount') and 'amazon_price' not in updates:
                updates['amazon_price'] = float(o['price']['Amount'])

        # ══════════════════════════════════════════════════
        # Write all updates at once
        # ══════════════════════════════════════════════════
        updates['last_sync_date'] = fields.Datetime.now()

        # Auto-download main image from URL (always re-download on pull)
        img_url = updates.get('image_url')
        if img_url:
            import base64
            import requests as req
            try:
                resp = req.get(img_url, timeout=15)
                resp.raise_for_status()
                if resp.content:
                    updates['product_image'] = base64.b64encode(resp.content)
                    _logger.info("Downloaded main image from %s", img_url)
            except Exception as exc:
                _logger.warning("Could not download image from %s: %s", img_url, exc)

        # Pull all images into image_ids
        all_image_urls = []
        if img_url:
            all_image_urls.append(img_url)
        # From summaries — additional images
        for s in data.get('summaries', []):
            for img in s.get('images', []):
                link = img.get('link', '')
                if link and link not in all_image_urls:
                    all_image_urls.append(link)
        # From attributes — main + other image locators
        a = data.get('attributes', {})
        if a:
            for key in ['main_product_image_locator'] + ['other_product_image_locator_%d' % i for i in range(1, 9)]:
                for img in a.get(key, []):
                    link = img.get('media_location', '')
                    if link and link not in all_image_urls:
                        all_image_urls.append(link)

        if all_image_urls:
            self.image_ids.unlink()
            for i, url in enumerate(all_image_urls[:9]):
                img_vals = {
                    'product_id': self.id,
                    'name': 'MAIN' if i == 0 else 'Image %d' % (i + 1),
                    'image_url': url,
                    'sequence': (i + 1) * 10,
                }
                # Download each image
                try:
                    resp = req.get(url, timeout=15)
                    resp.raise_for_status()
                    if resp.content:
                        img_vals['image'] = base64.b64encode(resp.content)
                except Exception:
                    _logger.warning("Could not download image %d from %s", i + 1, url)
                self.env['amazon.product.image'].create(img_vals)

        # Filter out Selection fields with invalid values
        updates = self._sanitize_selection_values(updates)

        _logger.info("Writing %d fields for SKU %s: %s", len(updates), self.sku, list(updates.keys()))
        self.write(updates)

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Pull from Amazon",
                "message": "Pulled %d fields for %s" % (len(updates), self.sku),
                "type": "success",
                "sticky": False,
            },
        }

    # ══════════════════════════════════════════════════
    # Pull Helpers
    # ══════════════════════════════════════════════════

    def _extract_value(self, attr_list, sub_key='value'):
        """Extract value from Amazon attribute list format.
        Amazon returns: [{"value": "X", "marketplace_id": "..."}, ...] or [{"value": X}]
        """
        if not attr_list or not isinstance(attr_list, list):
            return None
        first = attr_list[0]
        if isinstance(first, dict):
            return first.get(sub_key, first.get('value'))
        return str(first)

    def _pull_val(self, attributes, amazon_key, updates, odoo_field, sub_key='value'):
        """Pull a single text/selection value from Amazon attributes."""
        vals = attributes.get(amazon_key, [])
        extracted = self._extract_value(vals, sub_key)
        if extracted:
            updates[odoo_field] = str(extracted)

    def _pull_multi(self, attributes, amazon_key, updates, odoo_field):
        """Pull multi-value attribute as comma-separated string."""
        vals = attributes.get(amazon_key, [])
        if not vals or not isinstance(vals, list):
            return
        texts = []
        for v in vals:
            if isinstance(v, dict):
                text = v.get('value', '')
            else:
                text = str(v)
            if text:
                texts.append(text)
        if texts:
            updates[odoo_field] = ', '.join(texts)

    def _pull_numeric(self, attributes, amazon_key, updates, odoo_field):
        """Pull a numeric value."""
        vals = attributes.get(amazon_key, [])
        extracted = self._extract_value(vals)
        if extracted is not None:
            try:
                updates[odoo_field] = float(extracted)
            except (ValueError, TypeError):
                pass

    def _pull_dimension(self, attributes, amazon_key, updates, value_field, unit_field):
        """Pull a dimension value with unit."""
        vals = attributes.get(amazon_key, [])
        if vals and isinstance(vals[0], dict):
            d = vals[0]
            val = d.get('value')
            if val:
                try:
                    updates[value_field] = float(val)
                except (ValueError, TypeError):
                    pass
            unit = d.get('unit', '')
            valid_units = ('centimeters', 'inches', 'meters', 'millimeters', 'feet')
            if unit in valid_units:
                updates[unit_field] = unit

    def _sanitize_selection_values(self, updates):
        """Remove values that don't match Selection field options."""
        clean = {}
        for field_name, value in updates.items():
            field_def = self._fields.get(field_name)
            if field_def and field_def.type == 'selection':
                # Get valid selection keys
                if callable(field_def.selection):
                    try:
                        valid_keys = [k for k, v in field_def.selection(self)]
                    except Exception:
                        valid_keys = []
                else:
                    valid_keys = [k for k, v in (field_def.selection or [])]
                if value in valid_keys:
                    clean[field_name] = value
                else:
                    _logger.warning(
                        "Skipping field %s: value '%s' not in valid options %s",
                        field_name, value, valid_keys[:5]
                    )
            else:
                clean[field_name] = value
        return clean


class AmazonProductBulletPoint(models.Model):
    _name = 'amazon.product.bullet.point'
    _description = 'Amazon Product Bullet Point'
    _order = 'sequence, id'

    product_id = fields.Many2one('amazon.product', string='Product', required=True, ondelete='cascade')
    name = fields.Text('Bullet Point', required=True)
    sequence = fields.Integer('Sequence', default=10)


class AmazonProductImage(models.Model):
    _name = 'amazon.product.image'
    _description = 'Amazon Product Image'
    _order = 'sequence, id'

    product_id = fields.Many2one('amazon.product', string='Product', required=True, ondelete='cascade')
    sequence = fields.Integer('Sequence', default=10, help="First image = Main image")
    name = fields.Char('Label', default='Image')
    image = fields.Binary('Image', attachment=True)
    image_url = fields.Char('Image URL', help="External URL. Used if no file uploaded.")
    is_main = fields.Boolean('Main Image', compute='_compute_is_main', store=True)

    @api.depends('sequence', 'product_id.image_ids')
    def _compute_is_main(self):
        for rec in self:
            siblings = rec.product_id.image_ids.sorted('sequence')
            rec.is_main = bool(siblings and siblings[0].id == rec.id)
