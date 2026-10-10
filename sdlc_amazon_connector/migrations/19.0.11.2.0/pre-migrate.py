import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Protect operator-controlled cron configuration across module upgrades.

    Before 19.0.11.2.0 the 33 scheduled actions in data/cron.xml were loaded as
    non-noupdate records (ir.model.data.noupdate = FALSE). Every module upgrade
    therefore re-imported cron.xml and overwrote operator settings
    (active / interval_number / interval_type / priority) back to the XML
    defaults, which could silently re-activate a cron an operator had disabled.

    From this version the XML carries noupdate="1" so FRESH installs stamp these
    records noupdate=TRUE at creation. EXISTING databases keep noupdate=FALSE,
    though, because Odoo only sets noupdate at record creation — so we flip the
    flag here. This PRE-migrate runs BEFORE the cron.xml data reload in this very
    upgrade (odoo/modules/loading.py: migrate_module(pre) precedes load_data),
    so the operator's cron configuration is preserved on this upgrade and every
    future one. The update is idempotent and strictly scoped to this module's
    ir.cron records; no cron active state, interval, or nextcall is changed.
    """
    if not version:
        return
    cr.execute("""
        UPDATE ir_model_data
           SET noupdate = TRUE
         WHERE module = 'sdlc_amazon_connector'
           AND model = 'ir.cron'
           AND noupdate = FALSE
    """)
    _logger.info(
        "Phase 17 cron upgrade-safety: marked %s sdlc_amazon_connector ir.cron "
        "record(s) noupdate=TRUE before data reload.", cr.rowcount,
    )
