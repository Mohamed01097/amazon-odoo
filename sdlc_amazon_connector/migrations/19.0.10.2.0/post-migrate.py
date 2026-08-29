def migrate(cr, version):
    """Make already-mapped FBA products stockable without moving any stock."""
    cr.execute("""
        UPDATE product_template AS template
           SET is_storable = TRUE
          FROM product_product AS product,
               amazon_product AS amazon_product
         WHERE product.product_tmpl_id = template.id
           AND amazon_product.odoo_product_id = product.id
           AND amazon_product.fulfillment_channel = 'AFN'
           AND NOT COALESCE(template.is_storable, FALSE)
    """)
