import logging

from odoo import api, SUPERUSER_ID


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Add the sold/customer location without changing any stock quantity."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    instances = env['amazon.instance'].with_context(active_test=False).search([
        ('fba_warehouse_id', '!=', False),
        ('fba_source_location_id', '!=', False),
    ])
    for instance in instances:
        try:
            with cr.savepoint():
                instance.action_create_fba_stock_structure()
        except Exception as exc:
            _logger.warning(
                "Could not add the FBA sold/customer location for Amazon instance %s: %s",
                instance.display_name, exc,
            )
